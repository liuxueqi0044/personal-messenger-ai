"""Independent adversarial tests for the QQ VM selection-attestation authority.

These tests are deliberately self-contained: they build the smallest fake
accessibility / certifier / locator surface the worker actually consults instead
of importing helpers from sibling test modules.  No production behaviour is
modified and no assertion elsewhere is weakened.

Threat model under test
-----------------------
A *valid* ``SelectionHandoff`` is not by itself proof that the target row is the
selected one.  The visible chat header can be spoofed to the same display name,
so any shortcut that skips the ordinary discovery/selection path must still be
re-attested visually, twice, around the header read, and must fail closed the
moment the row is not provably selected.

Coverage map
------------
1. Valid handoff + spoofable same-name header but failing/unselected visual
   attestation: OBSERVE and PREPARE must not read bubbles, write the composer,
   send, or select again.
2. Valid handoff visual attestation must run twice (before/after the header
   read); a PID / HWND / runtime-digest / profile / context drift fails closed.
3. Invalid / replayed / cross-binding tokens trigger no header, visual, current
   read or selection action.
4. Only the exact COMMIT (same operation_id + binding + revisions) may reuse a
   PREPARE operation lease; a plain OBSERVE or another binding cannot inherit it.
5. A COMMIT deadline expiring after the composer check but before ``invoke_send``
   must not send and must return UNCERTAIN; a PREPARE deadline expiring before
   the write must not touch the composer.
6. Header or visual drift between PREPARE and COMMIT forbids ``invoke_send``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

import messenger_ai.adapters.qq.vm_driver.worker as worker_module
from messenger_ai.adapters.qq import BubbleDirection, QQBubble
from messenger_ai.adapters.qq.models import (
    QQConversation,
    QQIdentityBinding,
    QQSelector,
    QQSelectorPack,
    QQSessionObservedDirectIdentity,
    QQWindow,
)
from messenger_ai.adapters.qq.vm_driver import (
    QQVMWorker,
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
    mint_selection_handoff,
    session_identity,
)
from messenger_ai.adapters.qq.vm_driver.session_identity import (
    QQSessionCandidateLocator,
    QQSessionIdentityCertifier,
)
from messenger_ai.adapters.qq.vm_driver.visual_selection import (
    QQ_VM_ROW_PALETTE_PROFILE,
    RowPaletteSummary,
    ScreenRect,
    SelectionVisualAttestation,
    runtime_id_digest,
)

# ---------------------------------------------------------------------------
# Minimal self-contained fakes
# ---------------------------------------------------------------------------


class _FakeAccessibility:
    """The bounded UIA surface the tested worker code paths consult."""

    def __init__(self, window: QQWindow) -> None:
        self.window = window
        self.digest = "tree-v1"
        self.composer = ""
        self.bubbles: list[QQBubble] = []
        self.conversations: list[QQConversation] = []
        self.calls: list[str] = []
        self.emit_receipt = False
        self.select_result: object = None
        self.read_phase = None

    def find_main_windows(self, selector):
        self.calls.append("find")
        return [self.window]

    def list_conversations(self, window, selector):
        self.calls.append("conversations")
        return [
            item.model_copy(update={"tree_digest": self.digest})
            for item in self.conversations
        ]

    def select_conversation(self, window, conversation, selector):
        self.calls.append("select")
        return self.select_result

    def confirm_conversation_selected(self, window, conversation, selector):
        self.calls.append("confirm")

    def write_composer(self, window, text, selector):
        self.calls.append("write-composer")
        self.composer = text

    def read_composer(self, window, selector):
        self.calls.append("read-composer")
        return self.composer

    def invoke_send(self, window, selector):
        self.calls.append("invoke-send")
        if self.emit_receipt:
            self.bubbles.append(
                QQBubble(
                    conversation_internal_id=self.conversations[0].internal_id,
                    message_key=f"out-{len(self.bubbles)}",
                    direction=BubbleDirection.OUTBOUND,
                    text=self.composer,
                    observed_at=datetime.now(UTC) + timedelta(seconds=1),
                    tree_digest=self.digest,
                )
            )

    def list_bubbles(self, window, selector):
        self.calls.append("bubbles")
        return list(self.bubbles)

    def message_tail_is_latest(self, window, selector):
        return True

    def _window(self, window):
        return object()

    def _descendants(self, root):
        return []

    def _property(self, item, name, default=""):
        return default


class _CertifierRecorder:
    """Proxy around the real session certifier that records every consultation."""

    def __init__(self, inner: QQSessionIdentityCertifier, order: list[str]) -> None:
        self._inner = inner
        self._order = order
        self.try_calls = 0
        self.certify_calls = 0

    def try_certify_already_current(self, window, conversation):
        self.try_calls += 1
        self._order.append("header")
        return self._inner.try_certify_already_current(window, conversation)

    def certify_current(self, window, conversation):
        self.certify_calls += 1
        return self._inner.certify_current(window, conversation)


class _VisualAttestationController:
    """Fake local palette attestation with per-call drift injection."""

    def __init__(self, worker: QQVMWorker, fake: _FakeAccessibility,
                 order: list[str]) -> None:
        self.profile = QQ_VM_ROW_PALETTE_PROFILE.model_copy(update={
            "client_version": worker._selectors.client_version,
            "selector_pack_version": worker._selectors.fixture_suite_version,
            "environment_fingerprint": worker._selectors.environment_fingerprint,
        })
        self._order = order
        self.calls = 0
        self.failure: BaseException | None = None
        self.fail_on_call: int | None = None
        self.mutations: dict[int, dict[str, object]] = {}
        fake.certify_conversation_selected_visual = self._certify

    def next_call(self) -> int:
        return self.calls + 1

    def _attestation(self, window: QQWindow,
                     conversation: QQConversation) -> SelectionVisualAttestation:
        return SelectionVisualAttestation(
            profile_id=self.profile.profile_id,
            client_version=self.profile.client_version,
            selector_pack_version=self.profile.selector_pack_version,
            environment_fingerprint=self.profile.environment_fingerprint,
            process_id=window.process_id,
            window_handle=window.window_handle,
            target_runtime_id_digest=runtime_id_digest(conversation.internal_id),
            row_rect=ScreenRect(left=56, top=100, right=306, bottom=164),
            sample_count=2,
            stable_sample_count=2,
            unselected_control_count=2,
            selected=RowPaletteSummary(
                dominant_rgb=(225, 225, 225),
                ratio=0.779592,
                unique_count=12,
                pixel_count=5880,
            ),
            unselected=RowPaletteSummary(
                dominant_rgb=(245, 245, 245),
                ratio=1.0,
                unique_count=1,
                pixel_count=5880,
            ),
        )

    def _certify(self, window, conversation, _selector, actual_profile, *, deadline=None):
        assert actual_profile == self.profile
        assert deadline is None or deadline.tzinfo is not None
        self.calls += 1
        self._order.append("attest")
        call = self.calls
        if self.failure is not None and self.fail_on_call in (None, call):
            raise self.failure
        attestation = self._attestation(window, conversation)
        updates = self.mutations.get(call)
        if updates:
            attestation = attestation.model_copy(update=updates)
        return attestation


class _FrozenClock:
    """Manually advanced wall clock for deterministic deadline tests."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def advance(self, seconds: float) -> None:
        self.value = self.value + timedelta(seconds=seconds)


class _FakeDateTime:
    """Stand-in for the worker module's ``datetime`` symbol, driven by a clock."""

    clock: _FrozenClock

    @classmethod
    def now(cls, tz=None):
        return cls.clock.value


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _selector_pack() -> QQSelectorPack:
    return QQSelectorPack(
        client_version="9.9.33.51802",
        environment_fingerprint="e" * 64,
        selectors=tuple(
            QQSelector(name=name, automation_id=f"id-{name}", control_type="Pane")
            for name in (
                "main_window",
                "conversations",
                "conversation_item",
                "composer",
                "send",
                "bubbles",
            )
        ),
        last_verified_at=datetime.now(UTC),
        fixture_suite_version="qq-fake-v1",
    )


def _session_harness(monkeypatch, *, bindings_extra=()):
    """Bootstrapped direct session whose registered target may already be current."""

    pack = _selector_pack()
    window = QQWindow(process_id=101, window_handle=1001, class_name="QQNT")
    fake = _FakeAccessibility(window)
    proof = QQSessionObservedDirectIdentity(
        binding_id="session-contact-1",
        conversation_type="direct",
        type_evidence_source="operator_observed_direct",
        client_version=pack.client_version,
        selector_pack_version=pack.fixture_suite_version,
        group_marker_probe_complete=True,
        group_marker_count=0,
        process_id=window.process_id,
        window_handle=window.window_handle,
        process_started_at_100ns=123,
        vm_environment_fingerprint=pack.environment_fingerprint,
        selected_row_runtime_id_hash="b" * 64,
        header_digest="c" * 64,
    )
    fake.conversations = [
        QQConversation(
            internal_id="runtime:" + proof.selected_row_runtime_id_hash,
            participant_signature="uncertified:session-row",
            tree_digest=fake.digest,
        )
    ]
    binding = QQIdentityBinding(
        hub_conversation_id="hub-conv-1",
        contact_id="contact-1",
        account_id="account-1",
        platform_conversation_id=fake.conversations[0].internal_id,
        participant_signature=proof.participant_signature,
        binding_id=proof.binding_id,
        conversation_type="direct",
        authorization_scope="all_direct_including_temporary",
    )

    @contextmanager
    def read_phase(_window):
        yield object()

    fake.read_phase = read_phase
    monkeypatch.setattr(
        session_identity, "_process_started_at_100ns",
        lambda _pid: proof.process_started_at_100ns,
    )
    header = {"digest": proof.header_digest}
    monkeypatch.setattr(
        session_identity, "_header_digest", lambda *_args, **_kwargs: header["digest"]
    )

    order: list[str] = []
    real_certifier = QQSessionIdentityCertifier(
        accessibility=fake, selector_pack=pack, evidence=(proof,)
    )
    certifier = _CertifierRecorder(real_certifier, order)
    worker = QQVMWorker(
        accessibility=fake,
        selector_pack=pack,
        bindings=(binding, *bindings_extra),
        identity_certifier=certifier,
        candidate_locator=QQSessionCandidateLocator((proof,)),
        expected_window=real_certifier.window_scope,
        window_validator=real_certifier.validate_window,
    )
    visual = _VisualAttestationController(worker, fake, order)
    worker._selection_visual_profile = visual.profile
    return SimpleNamespace(
        worker=worker,
        fake=fake,
        binding=binding,
        proof=proof,
        visual=visual,
        certifier=certifier,
        header=header,
        order=order,
    )


def _other_binding() -> QQIdentityBinding:
    return QQIdentityBinding(
        hub_conversation_id="hub-conv-2",
        contact_id="contact-2",
        account_id="account-1",
        platform_conversation_id="runtime:other-row",
        participant_signature="qq-session-observed:other",
        binding_id="approved-binding-2",
        conversation_type="direct",
        authorization_scope="all_direct_including_temporary",
    )


# ---------------------------------------------------------------------------
# Command / handoff builders
# ---------------------------------------------------------------------------


def _observe_command(binding_id: str, **kwargs) -> WorkerCommand:
    return WorkerCommand(kind=WorkerKind.OBSERVE, binding_id=binding_id, **kwargs)


def _prepare_command(binding_id: str, *, operation_id=None,
                     revisions=(2, 3)) -> WorkerCommand:
    return WorkerCommand(
        kind=WorkerKind.PREPARE,
        binding_id=binding_id,
        binding_revision=revisions[0],
        conversation_revision=revisions[1],
        operation_id=operation_id or uuid4(),
        segment_ref="segment-1",
        text="hello there",
    )


def _commit_command(binding_id: str, operation_id, *,
                    revisions=(2, 3)) -> WorkerCommand:
    return WorkerCommand(
        kind=WorkerKind.COMMIT,
        binding_id=binding_id,
        binding_revision=revisions[0],
        conversation_revision=revisions[1],
        operation_id=operation_id,
    )


def _mint_refresh_handoff(harness, command: WorkerCommand, *, expires_at=None):
    """Mint the exact selection-refresh authority for ``command``."""

    successor = command.model_copy(
        update={"deadline": command.deadline or datetime.now(UTC) + timedelta(seconds=30)}
    )
    predecessor = successor.model_copy(
        update={"selection_handoff": None, "request_id": uuid4()}
    )
    result = WorkerResult(
        request_id=predecessor.request_id,
        kind=predecessor.kind,
        status=WorkerStatus.FAILED_SAFE,
        worker_epoch=uuid4(),
        operation_id=predecessor.operation_id,
        binding_id=predecessor.binding_id,
        binding_revision=predecessor.binding_revision,
        conversation_revision=predecessor.conversation_revision,
        error_code="selection_process_refresh_required",
    )
    return mint_selection_handoff(
        predecessor_command=predecessor,
        predecessor_result=result,
        successor_command=successor,
        source="selection_refresh",
        successor_worker_epoch=harness.worker._epoch,
        target_runtime_id_digest=runtime_id_digest(
            harness.binding.platform_conversation_id
        ),
        expires_at=expires_at or successor.deadline,
        signing_key=harness.worker._selection_handoff_signing_key,
    )


def _with_handoff(command: WorkerCommand, handoff) -> WorkerCommand:
    return command.model_copy(update={
        "selection_handoff": handoff,
        "deadline": handoff.successor_deadline,
    })


def _assert_no_authority_use(harness) -> None:
    """No header proof, visual attestation, current read or selection action."""

    assert harness.visual.calls == 0
    assert harness.certifier.try_calls == 0
    assert "bubbles" not in harness.fake.calls
    assert "select" not in harness.fake.calls
    assert "write-composer" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls


@pytest.mark.parametrize("mode", ["attestation_failed", "wrong_row"])
def test_plain_already_selected_observe_requires_palette_proof_before_bubbles(
    monkeypatch, mode,
) -> None:
    """Stale UIA selected/header state cannot authorize the visible panel."""

    harness = _session_harness(monkeypatch)
    harness.fake.select_result = None
    if mode == "attestation_failed":
        harness.visual.failure = RuntimeError("palette unproven")
        expected = "selection_visual_attestation_failed"
    else:
        harness.visual.mutations[1] = {
            "target_runtime_id_digest": runtime_id_digest("unregistered-same-name-row")
        }
        expected = "selection_visual_attestation_drift"

    result = harness.worker.execute(
        _observe_command(harness.binding.binding_id)
    )

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == expected
    assert "select" in harness.fake.calls
    assert "bubbles" not in harness.fake.calls
    assert "write-composer" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls


def test_plain_already_selected_observe_double_attests_around_bubble_read(
    monkeypatch,
) -> None:
    harness = _session_harness(monkeypatch)
    harness.fake.select_result = None

    result = harness.worker.execute(
        _observe_command(harness.binding.binding_id)
    )

    assert result.status is WorkerStatus.OK
    assert harness.visual.calls == 2
    assert harness.certifier.try_calls == 1
    assert harness.order == ["attest", "header", "attest"]
    assert harness.fake.calls.count("bubbles") == 1


# ---------------------------------------------------------------------------
# 1. Valid handoff but the row is not provably selected.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", [WorkerKind.OBSERVE, WorkerKind.PREPARE])
@pytest.mark.parametrize("mode", ["attestation_failed", "wrong_row"])
def test_valid_handoff_without_a_proven_row_never_reads_or_acts(
    monkeypatch, kind, mode
) -> None:
    harness = _session_harness(monkeypatch)
    if kind is WorkerKind.OBSERVE:
        command = _observe_command(harness.binding.binding_id)
    else:
        command = _prepare_command(harness.binding.binding_id)
    command = _with_handoff(command, _mint_refresh_handoff(harness, command))

    if mode == "attestation_failed":
        # The palette proof cannot show the target row as the selected one.
        harness.visual.failure = RuntimeError("target row is not the selected palette")
        expected = "selection_visual_attestation_failed"
    else:
        # The proof is bound to a different (still unselected) row.
        harness.visual.mutations[1] = {
            "target_runtime_id_digest": runtime_id_digest("some-other-row"),
        }
        expected = "selection_visual_attestation_drift"

    result = harness.worker.execute(command)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == expected
    # The (spoofable) live header is never even consulted.
    assert harness.certifier.try_calls == 0
    assert "bubbles" not in harness.fake.calls
    assert harness.fake.composer == ""
    assert "write-composer" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls
    assert "select" not in harness.fake.calls
    assert harness.worker._trusted_operation_lease is None


# ---------------------------------------------------------------------------
# 2. Visual attestation runs twice, around the header read, and drift fails.
# ---------------------------------------------------------------------------


def test_valid_handoff_attests_twice_around_the_header_read(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    command = _observe_command(harness.binding.binding_id)
    command = _with_handoff(command, _mint_refresh_handoff(harness, command))

    result = harness.worker.execute(command)

    assert result.status is WorkerStatus.OK
    assert harness.visual.calls == 2
    assert harness.certifier.try_calls == 1
    assert harness.order == ["attest", "header", "attest"]
    assert "select" not in harness.fake.calls


@pytest.mark.parametrize(
    ("label", "updates"),
    [
        ("process_id", {"process_id": 999_999}),
        ("window_handle", {"window_handle": 888_888}),
        ("runtime_digest", {"target_runtime_id_digest": runtime_id_digest("other-row")}),
        ("profile", {"profile_id": "some-other-profile"}),
        ("context", {"environment_fingerprint": "d" * 64}),
    ],
)
def test_visual_attestation_drift_between_the_two_calls_fails_closed(
    monkeypatch, label, updates
) -> None:
    harness = _session_harness(monkeypatch)
    command = _observe_command(harness.binding.binding_id)
    command = _with_handoff(command, _mint_refresh_handoff(harness, command))
    # Only the post-header attestation drifts from the pre-header one.
    harness.visual.mutations[2] = updates

    result = harness.worker.execute(command)

    assert harness.visual.calls == 2, label
    assert harness.certifier.try_calls == 1, label
    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_visual_attestation_drift"
    # The drift is caught before any UI action is taken.  (For OBSERVE the
    # current read is intentionally bounded between the two attestations, so a
    # message snapshot may already have been read; nothing else may be.)
    assert "select" not in harness.fake.calls
    assert "write-composer" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls
    assert harness.worker._trusted_operation_lease is None


# ---------------------------------------------------------------------------
# 3. Invalid / replayed / cross-binding tokens are inert.
# ---------------------------------------------------------------------------


def test_mismatched_handoff_kind_triggers_no_authority_use(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    command = _observe_command(harness.binding.binding_id)
    handoff = _mint_refresh_handoff(harness, command).model_copy(
        update={"target_kind": WorkerKind.VERIFY}
    )

    result = harness.worker.execute(_with_handoff(command, handoff))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_handoff_invalid"
    _assert_no_authority_use(harness)


def test_cross_binding_handoff_triggers_no_authority_use(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    command = _observe_command(harness.binding.binding_id)
    handoff = _mint_refresh_handoff(harness, command).model_copy(
        update={"binding_id": "some-other-binding"}
    )

    result = harness.worker.execute(_with_handoff(command, handoff))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_handoff_invalid"
    _assert_no_authority_use(harness)


def test_expired_handoff_triggers_no_authority_use(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    command = _observe_command(harness.binding.binding_id)
    handoff = _mint_refresh_handoff(harness, command).model_copy(
        update={"expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )

    result = harness.worker.execute(_with_handoff(command, handoff))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_handoff_invalid"
    _assert_no_authority_use(harness)


def test_replayed_handoff_is_spent_and_inert(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    command = _observe_command(harness.binding.binding_id)
    command = _with_handoff(command, _mint_refresh_handoff(harness, command))

    assert harness.worker.execute(command).status is WorkerStatus.OK
    baseline = (harness.visual.calls, harness.certifier.try_calls)
    harness.fake.calls.clear()

    replay = harness.worker.execute(command)

    assert replay.status is WorkerStatus.FAILED_SAFE
    assert replay.error_code == "selection_handoff_invalid"
    assert (harness.visual.calls, harness.certifier.try_calls) == baseline
    assert "bubbles" not in harness.fake.calls
    assert "select" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls


# ---------------------------------------------------------------------------
# 4. The operation lease is exact and cannot be inherited.
# ---------------------------------------------------------------------------


def test_prepare_leases_only_the_exact_commit(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    binding = harness.binding
    prepare = _prepare_command(binding.binding_id)
    assert harness.worker.execute(prepare).status is WorkerStatus.OK
    assert harness.worker._trusted_operation_lease == (
        prepare.operation_id,
        binding.binding_id,
        prepare.binding_revision,
        prepare.conversation_revision,
    )

    exact = _commit_command(binding.binding_id, prepare.operation_id)
    assert harness.worker._operation_lease_matches(exact, binding) is True
    # A plain OBSERVE for the same operation can never inherit the lease.
    observe = _observe_command(binding.binding_id, operation_id=prepare.operation_id)
    assert harness.worker._operation_lease_matches(observe, binding) is False
    # A drifted binding or conversation revision cannot inherit it either.
    drifted_binding = _commit_command(
        binding.binding_id, prepare.operation_id,
        revisions=(prepare.binding_revision + 1, prepare.conversation_revision),
    )
    assert harness.worker._operation_lease_matches(drifted_binding, binding) is False
    drifted_conversation = _commit_command(
        binding.binding_id, prepare.operation_id,
        revisions=(prepare.binding_revision, prepare.conversation_revision + 1),
    )
    assert harness.worker._operation_lease_matches(
        drifted_conversation, binding
    ) is False
    # A COMMIT naming a different operation cannot inherit it.
    other_operation = _commit_command(binding.binding_id, uuid4())
    assert harness.worker._operation_lease_matches(other_operation, binding) is False


def test_exact_commit_reuses_the_lease_without_reselecting(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    assert harness.worker.execute(prepare).status is WorkerStatus.OK
    harness.fake.calls.clear()
    attest_before = harness.visual.calls

    commit = _commit_command(harness.binding.binding_id, prepare.operation_id)
    result = harness.worker.execute(commit)

    assert result.status is WorkerStatus.OK
    assert "invoke-send" in harness.fake.calls
    assert "select" not in harness.fake.calls
    assert harness.visual.calls > attest_before


def test_drifted_revision_commit_is_refused_without_selecting(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    assert harness.worker.execute(prepare).status is WorkerStatus.OK
    harness.fake.calls.clear()
    attest_before = harness.visual.calls

    commit = _commit_command(
        harness.binding.binding_id, prepare.operation_id,
        revisions=(prepare.binding_revision, prepare.conversation_revision + 1),
    )
    result = harness.worker.execute(commit)

    # A COMMIT that cannot prove its operation lease is outcome-unknown, so it
    # is reported conservatively; the essential guarantee is that nothing ran.
    assert result.status is WorkerStatus.UNCERTAIN
    assert result.error_code == "operation_identity_lease_invalid"
    assert "select" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls
    assert harness.visual.calls == attest_before


def test_other_binding_commit_cannot_inherit_the_lease(monkeypatch) -> None:
    harness = _session_harness(monkeypatch, bindings_extra=(_other_binding(),))
    prepare = _prepare_command(harness.binding.binding_id)
    assert harness.worker.execute(prepare).status is WorkerStatus.OK
    harness.fake.calls.clear()
    attest_before = harness.visual.calls

    commit = _commit_command("approved-binding-2", prepare.operation_id)
    result = harness.worker.execute(commit)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "operation_binding_mismatch"
    assert "select" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls
    assert harness.visual.calls == attest_before


def test_ordinary_observe_cannot_inherit_the_lease(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    assert harness.worker.execute(prepare).status is WorkerStatus.OK
    harness.fake.calls.clear()
    harness.fake.select_result = True  # make the ordinary select path observable
    attest_before = harness.visual.calls

    observe = _observe_command(
        harness.binding.binding_id, operation_id=prepare.operation_id
    )
    result = harness.worker.execute(observe)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_process_refresh_required"
    assert "select" in harness.fake.calls
    assert harness.visual.calls == attest_before


# ---------------------------------------------------------------------------
# 5. Deadlines.
# ---------------------------------------------------------------------------


def test_commit_deadline_after_composer_check_never_sends(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    assert harness.worker.execute(prepare).status is WorkerStatus.OK

    clock = _FrozenClock(datetime.now(UTC))
    _FakeDateTime.clock = clock
    monkeypatch.setattr(worker_module, "datetime", _FakeDateTime)
    commit = _commit_command(harness.binding.binding_id, prepare.operation_id).model_copy(
        update={"deadline": clock.value + timedelta(seconds=10)}
    )

    original_read = harness.fake.read_composer

    def read_composer(window, selector):
        text = original_read(window, selector)
        clock.advance(30)  # expires only after the composer check has returned
        return text

    harness.fake.read_composer = read_composer
    harness.fake.calls.clear()

    result = harness.worker.execute(commit)

    assert result.status is WorkerStatus.UNCERTAIN
    assert result.error_code == "deadline_expired"
    assert "invoke-send" not in harness.fake.calls
    assert "read-composer" in harness.fake.calls


def test_prepare_deadline_before_write_never_touches_composer(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    clock = _FrozenClock(datetime.now(UTC))
    _FakeDateTime.clock = clock
    monkeypatch.setattr(worker_module, "datetime", _FakeDateTime)
    prepare = _prepare_command(harness.binding.binding_id).model_copy(
        update={"deadline": clock.value + timedelta(seconds=10)}
    )

    original_read = harness.fake.read_composer

    def read_composer(window, selector):
        text = original_read(window, selector)
        clock.advance(30)
        return text

    harness.fake.read_composer = read_composer

    result = harness.worker.execute(prepare)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "deadline_expired"
    assert harness.fake.composer == ""
    assert "write-composer" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls
    assert harness.worker._trusted_operation_lease is None


def test_prepare_requires_post_write_budget_before_touching_composer(
    monkeypatch,
) -> None:
    harness = _session_harness(monkeypatch)
    clock = _FrozenClock(datetime.now(UTC))
    _FakeDateTime.clock = clock
    monkeypatch.setattr(worker_module, "datetime", _FakeDateTime)
    prepare = _prepare_command(harness.binding.binding_id).model_copy(
        update={
            "deadline": clock.value
            + timedelta(
                seconds=harness.worker._prepare_write_reserve_seconds - 1
            )
        }
    )

    result = harness.worker.execute(prepare)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "prepare_write_budget_exhausted"
    assert result.evidence == {"composer_written": False}
    assert harness.fake.composer == ""
    assert "write-composer" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls
    assert harness.worker._reservation is None
    assert harness.worker._trusted_operation_lease is None


def test_prepare_post_write_identity_failure_remains_reserved_until_exact_abort(
    monkeypatch,
) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    # Calls 1-6 prove the selected target before the write. Call 7 is the
    # first post-write proof and is forced onto an unregistered same-name row.
    harness.visual.mutations[7] = {
        "target_runtime_id_digest": runtime_id_digest("unregistered-same-name-row")
    }

    failed = harness.worker.execute(prepare)

    assert failed.status is WorkerStatus.FAILED_SAFE
    assert failed.error_code == "selection_visual_attestation_drift"
    assert harness.fake.composer == prepare.text
    assert harness.worker._reservation == prepare.operation_id
    assert harness.worker._trusted_operation_lease == (
        prepare.operation_id,
        harness.binding.binding_id,
        prepare.binding_revision,
        prepare.conversation_revision,
    )
    assert "invoke-send" not in harness.fake.calls

    aborted = harness.worker.execute(WorkerCommand(
        kind=WorkerKind.ABORT,
        binding_id=harness.binding.binding_id,
        binding_revision=prepare.binding_revision,
        conversation_revision=prepare.conversation_revision,
        operation_id=prepare.operation_id,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    ))

    assert aborted.status is WorkerStatus.OK
    assert harness.fake.composer == ""
    assert harness.worker._reservation is None
    assert harness.worker._trusted_operation_lease is None


def test_prepare_partial_write_failure_is_not_claimed_clean(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)

    def partial_write(_window, text, _selector):
        harness.fake.calls.append("write-composer")
        harness.fake.composer = text[:3]
        raise RuntimeError("partial write")

    harness.fake.write_composer = partial_write
    failed = harness.worker.execute(prepare)

    assert failed.status is WorkerStatus.FAILED_SAFE
    assert harness.worker._reservation == prepare.operation_id
    aborted = harness.worker.execute(WorkerCommand(
        kind=WorkerKind.ABORT,
        binding_id=harness.binding.binding_id,
        binding_revision=prepare.binding_revision,
        conversation_revision=prepare.conversation_revision,
        operation_id=prepare.operation_id,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    ))
    assert aborted.status is WorkerStatus.FAILED_SAFE
    assert aborted.error_code == "needs_manual_cleanup"
    assert harness.fake.composer == prepare.text[:3]
    assert harness.worker._reservation == prepare.operation_id


def test_verify_bubble_read_drift_cannot_produce_a_receipt(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    harness.fake.emit_receipt = True
    prepare = _prepare_command(harness.binding.binding_id)
    assert harness.worker.execute(prepare).status is WorkerStatus.OK
    assert harness.worker.execute(
        _commit_command(harness.binding.binding_id, prepare.operation_id)
    ).status is WorkerStatus.OK

    original = harness.fake.list_bubbles

    def drifting_read(window, selector):
        rows = original(window, selector)
        harness.visual.mutations[harness.visual.next_call()] = {
            "target_runtime_id_digest": runtime_id_digest(
                "unregistered-same-name-row"
            )
        }
        return rows

    harness.fake.list_bubbles = drifting_read
    result = harness.worker.execute(WorkerCommand(
        kind=WorkerKind.VERIFY,
        binding_id=harness.binding.binding_id,
        binding_revision=prepare.binding_revision,
        conversation_revision=prepare.conversation_revision,
        operation_id=prepare.operation_id,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
    ))

    assert result.status is WorkerStatus.UNCERTAIN
    assert result.error_code == "selection_visual_attestation_drift"
    assert "receipt" not in result.evidence


# ---------------------------------------------------------------------------
# 6. Header / visual drift between PREPARE and COMMIT.
# ---------------------------------------------------------------------------


def test_header_drift_between_prepare_and_commit_never_sends(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    assert harness.worker.execute(prepare).status is WorkerStatus.OK
    harness.fake.calls.clear()

    # The live chat header now proves a different conversation.
    harness.header["digest"] = "f" * 64

    commit = _commit_command(harness.binding.binding_id, prepare.operation_id)
    result = harness.worker.execute(commit)

    assert result.status is WorkerStatus.UNCERTAIN
    assert result.error_code == "selection_visual_header_unproven"
    assert "invoke-send" not in harness.fake.calls
    assert "select" not in harness.fake.calls


def test_visual_drift_between_prepare_and_commit_never_sends(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    assert harness.worker.execute(prepare).status is WorkerStatus.OK
    harness.fake.calls.clear()

    # At COMMIT time the panel no longer shows the exact certified row.
    harness.visual.mutations[harness.visual.next_call()] = {
        "target_runtime_id_digest": runtime_id_digest("a-different-row"),
    }

    commit = _commit_command(harness.binding.binding_id, prepare.operation_id)
    result = harness.worker.execute(commit)

    assert result.status is WorkerStatus.UNCERTAIN
    assert result.error_code == "selection_visual_attestation_drift"
    assert "invoke-send" not in harness.fake.calls
    assert "select" not in harness.fake.calls


def test_observe_tail_scroll_recertifies_without_reusing_consumed_handoff(monkeypatch):
    harness = _session_harness(monkeypatch)
    command = _observe_command(harness.binding.binding_id)
    command = _with_handoff(command, _mint_refresh_handoff(harness, command))
    latest = False
    actions = []
    harness.fake.message_tail_is_latest = lambda *_: latest

    def scroll(*args, before_action):
        nonlocal latest
        before_action()
        actions.append("scroll")
        latest = True

    harness.fake.scroll_message_tail_to_latest = scroll
    result = harness.worker.execute(command)
    assert result.status is WorkerStatus.OK
    assert actions == ["scroll"]
    assert harness.visual.calls == 4
    assert harness.certifier.try_calls == 2
    assert harness.fake.calls.count("bubbles") == 1
    # The first capability is still spent, and cannot authorize another request.
    replay = harness.worker.execute(command)
    assert replay.status is WorkerStatus.FAILED_SAFE
    assert replay.error_code == "selection_handoff_invalid"
    assert actions == ["scroll"]
