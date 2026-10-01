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
        self.active_phase: int | None = None
        self.phase_count = 0

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
        self._fake = fake
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
        assert self._fake.active_phase is None
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
        assert fake.active_phase is None
        fake.phase_count += 1
        fake.active_phase = fake.phase_count
        try:
            yield object()
        finally:
            fake.active_phase = None

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


@pytest.mark.parametrize("use_handoff", [False, True])
def test_prepare_uses_one_fenced_content_snapshot_and_fresh_fenced_readback(
    monkeypatch, use_handoff,
) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    if use_handoff:
        prepare = _with_handoff(prepare, _mint_refresh_handoff(harness, prepare))
    anchor = QQBubble(
        conversation_internal_id=harness.fake.conversations[0].internal_id,
        message_key="inbound-anchor",
        direction=BubbleDirection.INBOUND,
        text="existing inbound",
        observed_at=datetime.now(UTC),
        tree_digest=harness.fake.digest,
    )
    harness.fake.bubbles = [anchor]
    read_phases = []
    for method, event in (("list_bubbles", "bubbles"), ("read_composer", "composer")):
        original = getattr(harness.fake, method)

        def record_read(*args, original=original, event=event):
            assert harness.fake.active_phase is not None
            read_phases.append(harness.fake.active_phase)
            harness.order.append(event)
            return original(*args)

        setattr(harness.fake, method, record_read)
    original_write = harness.fake.write_composer

    def record_write(*args):
        assert harness.fake.active_phase is None
        harness.order.append("write")
        original_write(*args)

    harness.fake.write_composer = record_write

    result = harness.worker.execute(prepare)

    assert result.status is WorkerStatus.OK
    assert harness.order == [
        "attest", "header", "bubbles", "composer", "attest", "write",
        "attest", "header", "composer", "attest",
    ]
    assert harness.visual.calls == 4
    assert harness.certifier.try_calls == 2
    assert harness.fake.calls.count("bubbles") == 1
    assert harness.fake.calls.count("read-composer") == 2
    assert harness.fake.calls.count("write-composer") == 1
    assert harness.fake.phase_count == 3  # discovery, content, fresh readback
    assert read_phases[0] == read_phases[1] != read_phases[2]
    assert harness.fake.active_phase is None
    assert result.evidence["prepared_evidence"]["before_bubbles"] == [{
        "direction": anchor.direction.value,
        "message_key": anchor.message_key,
        "conversation_internal_id": anchor.conversation_internal_id,
        "text_hash": anchor.text_hash,
    }]
    assert harness.worker._reservation == prepare.operation_id
    assert "invoke-send" not in harness.fake.calls


@pytest.mark.parametrize("read_stage", ["header", "bubbles", "composer"])
@pytest.mark.parametrize("updates", [
    {"process_id": 999_999},
    {"window_handle": 888_888},
    {"target_runtime_id_digest": runtime_id_digest("different-session")},
    {"profile_id": "different-profile"},
    {"environment_fingerprint": "d" * 64},
    {"row_rect": ScreenRect(left=56, top=164, right=306, bottom=228)},
])
def test_prepare_read_phase_drift_never_acquires_write_authority(
    monkeypatch, read_stage, updates,
) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    target, method = {
        "header": (harness.certifier, "try_certify_already_current"),
        "bubbles": (harness.fake, "list_bubbles"),
        "composer": (harness.fake, "read_composer"),
    }[read_stage]
    original = getattr(target, method)

    def read_then_drift(*args):
        value = original(*args)
        harness.visual.mutations[harness.visual.next_call()] = updates
        return value

    monkeypatch.setattr(target, method, read_then_drift)

    result = harness.worker.execute(prepare)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_visual_attestation_drift"
    assert harness.fake.composer == ""
    assert "write-composer" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls
    assert harness.worker._reservation is None
    assert harness.worker._trusted_operation_lease is None
    assert prepare.operation_id not in harness.worker._prepared
    assert harness.fake.active_phase is None


def test_prepare_draft_added_during_bubble_read_is_preserved(monkeypatch) -> None:
    harness = _session_harness(monkeypatch)
    original = harness.fake.list_bubbles

    def read_bubbles_then_new_draft(*args):
        rows = original(*args)
        harness.fake.composer = "new operator draft"
        return rows

    harness.fake.list_bubbles = read_bubbles_then_new_draft
    result = harness.worker.execute(_prepare_command(harness.binding.binding_id))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "composer_not_empty"
    assert harness.visual.calls == 2
    assert harness.fake.composer == "new operator draft"
    assert "write-composer" not in harness.fake.calls
    assert harness.worker._reservation is None
    assert harness.worker._trusted_operation_lease is None


def test_prepare_transport_rejection_of_new_draft_does_not_claim_clean(
    monkeypatch,
) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)

    def write_entry_rejects_new_draft(*_args):
        # Model the transport's fresh empty-composer guard after an external
        # edit between the complete read fence and the actual write entry.
        assert harness.fake.active_phase is None
        assert harness.visual.calls == 2
        harness.fake.composer = "late operator draft"
        raise worker_module.UIAUnavailable("composer_not_empty")

    harness.fake.write_composer = write_entry_rejects_new_draft
    result = harness.worker.execute(prepare)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "composer_not_empty"
    assert result.evidence["cleanup_required"] is True
    assert harness.fake.composer == "late operator draft"
    assert "write-composer" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls
    assert harness.worker._reservation == prepare.operation_id
    aborted = harness.worker.execute(WorkerCommand(
        kind=WorkerKind.ABORT,
        binding_id=harness.binding.binding_id,
        binding_revision=prepare.binding_revision,
        conversation_revision=prepare.conversation_revision,
        operation_id=prepare.operation_id,
    ))
    assert aborted.status is WorkerStatus.FAILED_SAFE
    assert aborted.error_code == "needs_manual_cleanup"
    assert harness.fake.composer == "late operator draft"
    assert harness.worker._reservation == prepare.operation_id


@pytest.mark.parametrize("remaining_above_reserve", [-1, 0, 1])
def test_prepare_checks_unchanged_reserve_after_the_complete_read_fence(
    monkeypatch, remaining_above_reserve,
) -> None:
    harness = _session_harness(monkeypatch)
    clock = _FrozenClock(datetime.now(UTC))
    _FakeDateTime.clock = clock
    monkeypatch.setattr(worker_module, "datetime", _FakeDateTime)
    prepare = _prepare_command(harness.binding.binding_id).model_copy(update={
        "deadline": clock.value + timedelta(seconds=100),
    })
    original = harness.fake.certify_conversation_selected_visual

    def attest_then_spend_budget(*args, **kwargs):
        attestation = original(*args, **kwargs)
        if harness.visual.calls == 2:
            clock.advance(
                100 - harness.worker._prepare_write_reserve_seconds
                - remaining_above_reserve
            )
        return attestation

    harness.fake.certify_conversation_selected_visual = attest_then_spend_budget
    result = harness.worker.execute(prepare)

    if remaining_above_reserve > 0:
        assert result.status is WorkerStatus.OK
        assert harness.fake.composer == prepare.text
    else:
        assert result.status is WorkerStatus.FAILED_SAFE
        assert result.error_code == "prepare_write_budget_exhausted"
        assert result.evidence == {"composer_written": False}
        assert "write-composer" not in harness.fake.calls
        assert harness.worker._reservation is None
        assert harness.worker._trusted_operation_lease is None
    assert "invoke-send" not in harness.fake.calls


@pytest.mark.parametrize("drift_moment", ["before_write", "after_write"])
def test_prepare_post_write_identity_failure_remains_reserved_until_exact_abort(
    monkeypatch, drift_moment,
) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    original_write = harness.fake.write_composer

    def write_then_drift(window, text, selector):
        if drift_moment == "after_write":
            original_write(window, text, selector)
        # Inject at the action boundary rather than depending on how many
        # complete read-only proofs preceded the write.
        harness.visual.mutations[harness.visual.next_call()] = {
            "target_runtime_id_digest": runtime_id_digest("unregistered-same-name-row")
        }
        if drift_moment == "before_write":
            # A same-window external conversation switch can occur after the
            # final read fence. The fresh post-write proof must catch it; a
            # previous snapshot can never authorize a successful PREPARE.
            original_write(window, text, selector)

    harness.fake.write_composer = write_then_drift

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

    harness.fake.write_composer = original_write
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


@pytest.mark.parametrize("drift", ["selection", "header", "composer"])
def test_prepare_post_write_readback_uses_fresh_state(monkeypatch, drift) -> None:
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    original_write = harness.fake.write_composer
    original_read = harness.fake.read_composer

    def write_then_change(*args):
        original_write(*args)
        if drift == "header":
            harness.header["digest"] = "f" * 64
        elif drift == "composer":
            harness.fake.composer = "changed after write"

    def read_then_change(*args):
        value = original_read(*args)
        if drift == "selection" and "write-composer" in harness.fake.calls:
            harness.visual.mutations[harness.visual.next_call()] = {
                "target_runtime_id_digest": runtime_id_digest("different-session"),
            }
        return value

    harness.fake.write_composer = write_then_change
    harness.fake.read_composer = read_then_change
    result = harness.worker.execute(prepare)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == {
        "selection": "selection_visual_attestation_drift",
        "header": "selection_visual_header_unproven",
        "composer": "composer_readback_mismatch",
    }[drift]
    assert result.evidence["cleanup_required"] is True
    assert harness.worker._reservation == prepare.operation_id
    assert "invoke-send" not in harness.fake.calls
    assert harness.fake.active_phase is None


def _move_geometry_during_first_content_read(harness):
    """A row moves once between complete proofs, then stays at its new rect."""
    moved = False
    original_bubbles = harness.fake.list_bubbles
    original_attest = harness.fake.certify_conversation_selected_visual
    new_rect = ScreenRect(left=56, top=164, right=306, bottom=228)

    def bubbles(*args):
        nonlocal moved
        rows = original_bubbles(*args)
        moved = True
        return rows

    def attest(*args, **kwargs):
        result = original_attest(*args, **kwargs)
        return result.model_copy(update={"row_rect": new_rect}) if moved else result

    harness.fake.list_bubbles = bubbles
    harness.fake.certify_conversation_selected_visual = attest


@pytest.mark.parametrize("use_handoff", [False, True])
@pytest.mark.parametrize("second_draft", ["", "new operator draft"])
def test_prepare_geometry_retry_discards_first_content_and_does_not_reresolve(
    monkeypatch, use_handoff, second_draft, tmp_path,
) -> None:
    from messenger_ai.adapters.qq.vm_driver.diagnostics import WorkerDiagnosticSink
    import json

    harness = _session_harness(monkeypatch)
    harness.worker._diagnostics = WorkerDiagnosticSink(tmp_path, "child")
    prepare = _prepare_command(harness.binding.binding_id)
    if use_handoff:
        prepare = _with_handoff(prepare, _mint_refresh_handoff(harness, prepare))
    _move_geometry_during_first_content_read(harness)
    original_bubbles = harness.fake.list_bubbles
    original_composer = harness.fake.read_composer
    original_header = harness.certifier.try_certify_already_current
    original_target = harness.worker._stable_target
    original_consume = harness.worker._consume_selection_handoff
    reads, proofs, target_proofs, handoffs = [], [], [], []

    def bubbles(*args):
        original_bubbles(*args)
        number = len(reads) + 1
        bubble = QQBubble(
            conversation_internal_id=harness.fake.conversations[0].internal_id,
            message_key=f"snapshot-{number}", direction=BubbleDirection.INBOUND,
            text=f"private-message-{number}", observed_at=datetime.now(UTC),
            tree_digest=harness.fake.digest,
        )
        reads.append((bubble, harness.fake.active_phase))
        return [bubble]

    def composer(*args):
        if len(reads) == 2 and "write-composer" not in harness.fake.calls:
            harness.fake.composer = second_draft
        return original_composer(*args)

    def header(*args):
        proof = original_header(*args).model_copy()
        proofs.append(proof)
        return proof

    def target(**kwargs):
        target_proofs.append(kwargs["proof"])
        return original_target(**kwargs)

    def consume(*args):
        handoffs.append(args)
        return original_consume(*args)

    harness.fake.list_bubbles = bubbles
    harness.fake.read_composer = composer
    harness.certifier.try_certify_already_current = header
    harness.worker._stable_target = target
    harness.worker._consume_selection_handoff = consume
    result = harness.worker.execute(prepare)

    assert len(reads) == 2
    assert reads[0][1] != reads[1][1]  # the discarded read phase is closed
    assert target_proofs[0] is proofs[1]
    assert all(item is not proofs[0] for item in target_proofs)
    assert len(handoffs) == int(use_handoff)
    assert harness.fake.calls.count("find") == 1
    assert harness.fake.calls.count("select") == int(not use_handoff)
    assert harness.fake.calls.count("conversations") == 1
    assert "invoke-send" not in harness.fake.calls
    if second_draft:
        assert result.status is WorkerStatus.FAILED_SAFE
        assert result.error_code == "composer_not_empty"
        assert harness.fake.composer == second_draft
        assert "write-composer" not in harness.fake.calls
        assert harness.worker._reservation is None
        assert harness.visual.calls == 4
    else:
        assert result.status is WorkerStatus.OK
        anchors = result.evidence["prepared_evidence"]["before_bubbles"]
        assert [item["message_key"] for item in anchors] == ["snapshot-2"]
        assert anchors[0]["text_hash"] == reads[1][0].text_hash
        assert harness.fake.composer == prepare.text
        assert harness.fake.calls.count("write-composer") == 1
        assert harness.visual.calls == 6  # retry plus fresh post-write proof
    logs = (tmp_path / "qq-worker-child.jsonl").read_text()
    events = [json.loads(line) for line in logs.splitlines()]
    diagnostics = [item["selection_attestation"] for item in events
                   if "selection_attestation" in item]
    assert diagnostics == [{
        "comparison": "before_after", "changed_fields": ["row_rect"],
        "before_rect": [56, 100, 306, 164], "after_rect": [56, 164, 306, 228],
        "attempt": 1, "retrying": True,
    }]
    assert "private-message" not in logs and "operator draft" not in logs


def test_prepare_second_geometry_drift_is_refused_with_bounded_diagnostic(monkeypatch):
    harness = _session_harness(monkeypatch)
    original = harness.fake.list_bubbles

    def bubbles_then_move(*args):
        result = original(*args)
        harness.visual.mutations[harness.visual.next_call()] = {
            "row_rect": ScreenRect(left=56, top=164, right=306, bottom=228),
        }
        return result

    harness.fake.list_bubbles = bubbles_then_move
    result = harness.worker.execute(_prepare_command(harness.binding.binding_id))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_visual_attestation_drift"
    assert result.evidence["selection_attestation"] == {
        "comparison": "before_after", "changed_fields": ["row_rect"],
        "before_rect": [56, 100, 306, 164], "after_rect": [56, 164, 306, 228],
        "attempt": 2, "retrying": False,
    }
    assert harness.visual.calls == 4
    assert harness.fake.calls.count("bubbles") == 2
    assert "write-composer" not in harness.fake.calls
    assert harness.worker._reservation is None


@pytest.mark.parametrize("mutation", [
    {"process_id": 202}, {"window_handle": 2002},
    {"target_runtime_id_digest": "d" * 64}, {"profile_id": "other-profile"},
    {"environment_fingerprint": "d" * 64}, {"client_version": "different"},
    {"selector_pack_version": "different"},
])
def test_prepare_geometry_plus_scope_drift_never_retries(monkeypatch, mutation):
    harness = _session_harness(monkeypatch)
    harness.visual.mutations[2] = {
        "row_rect": ScreenRect(left=56, top=164, right=306, bottom=228), **mutation,
    }
    result = harness.worker.execute(_prepare_command(harness.binding.binding_id))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_visual_attestation_drift"
    assert harness.visual.calls == 2
    assert harness.fake.calls.count("bubbles") == 1
    assert "write-composer" not in harness.fake.calls
    assert harness.worker._reservation is None
    assert result.evidence["selection_attestation"]["comparison"] == "expected_scope"
    assert result.evidence["selection_attestation"]["changed_fields"] == list(mutation)
    assert result.evidence["selection_attestation"]["retrying"] is False


def test_prepare_geometry_drift_with_first_draft_never_retries(monkeypatch):
    harness = _session_harness(monkeypatch)
    _move_geometry_during_first_content_read(harness)
    harness.fake.composer = "existing operator draft"
    result = harness.worker.execute(_prepare_command(harness.binding.binding_id))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_visual_attestation_drift"
    assert harness.visual.calls == 2
    assert harness.fake.calls.count("read-composer") == 1
    assert "write-composer" not in harness.fake.calls
    assert harness.fake.composer == "existing operator draft"


@pytest.mark.parametrize("changed_call", [3, 4])
def test_prepare_retry_identity_drift_has_second_attempt_diagnostic(monkeypatch, changed_call):
    harness = _session_harness(monkeypatch)
    _move_geometry_during_first_content_read(harness)
    harness.visual.mutations[changed_call] = {"window_handle": 2002}
    result = harness.worker.execute(_prepare_command(harness.binding.binding_id))

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_visual_attestation_drift"
    assert harness.visual.calls == changed_call
    assert result.evidence["selection_attestation"]["attempt"] == 2
    assert result.evidence["selection_attestation"]["changed_fields"] == ["window_handle"]
    assert result.evidence["selection_attestation"]["retrying"] is False
    assert "write-composer" not in harness.fake.calls
    assert harness.worker._reservation is None


@pytest.mark.parametrize("clock_boundary", ["before_retry", "during_retry", "reserve"])
def test_prepare_geometry_retry_uses_original_deadline_and_reserve(
    monkeypatch, clock_boundary,
):
    harness = _session_harness(monkeypatch)
    _move_geometry_during_first_content_read(harness)
    clock = _FrozenClock(datetime.now(UTC))
    _FakeDateTime.clock = clock
    monkeypatch.setattr(worker_module, "datetime", _FakeDateTime)
    prepare = _prepare_command(harness.binding.binding_id).model_copy(update={
        "deadline": clock.value + timedelta(seconds=100),
    })
    original_read = harness.fake.read_composer
    original_report = harness.worker._report_selection_attestation_drift
    reads = 0

    def read(*args):
        nonlocal reads
        reads += 1
        value = original_read(*args)
        if reads == 2:
            clock.advance(100 if clock_boundary == "during_retry" else
                          100 - harness.worker._prepare_write_reserve_seconds)
        return value

    def report(*args):
        original_report(*args)
        if clock_boundary == "before_retry":
            clock.advance(100)

    harness.fake.read_composer = read
    harness.worker._report_selection_attestation_drift = report
    result = harness.worker.execute(prepare)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == (
        "prepare_write_budget_exhausted" if clock_boundary == "reserve" else "deadline_expired"
    )
    assert reads == (1 if clock_boundary == "before_retry" else 2)
    assert "write-composer" not in harness.fake.calls
    assert harness.worker._reservation is None


def test_prepare_post_write_geometry_drift_never_retries(monkeypatch):
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    original = harness.fake.read_composer

    def read(*args):
        value = original(*args)
        if "write-composer" in harness.fake.calls:
            harness.visual.mutations[harness.visual.next_call()] = {
                "row_rect": ScreenRect(left=56, top=164, right=306, bottom=228),
            }
        return value

    harness.fake.read_composer = read
    result = harness.worker.execute(prepare)

    assert result.status is WorkerStatus.FAILED_SAFE
    assert result.error_code == "selection_visual_attestation_drift"
    assert harness.visual.calls == 4
    assert harness.fake.calls.count("bubbles") == 1
    assert harness.fake.calls.count("read-composer") == 2
    assert harness.fake.composer == prepare.text
    assert harness.worker._reservation == prepare.operation_id
    assert result.evidence["cleanup_required"] is True
    assert result.evidence["selection_attestation"]["retrying"] is False


@pytest.mark.parametrize("kind", [WorkerKind.OBSERVE, WorkerKind.COMMIT, WorkerKind.VERIFY, WorkerKind.ABORT])
def test_non_prepare_geometry_drift_never_retries(monkeypatch, kind):
    harness = _session_harness(monkeypatch)
    prepare = _prepare_command(harness.binding.binding_id)
    if kind is not WorkerKind.OBSERVE:
        assert harness.worker.execute(prepare).status is WorkerStatus.OK
    command = WorkerCommand(
        kind=kind, binding_id=harness.binding.binding_id,
        operation_id=prepare.operation_id if kind is not WorkerKind.OBSERVE else None,
        binding_revision=prepare.binding_revision,
        conversation_revision=prepare.conversation_revision,
    )
    before = harness.visual.calls
    harness.visual.mutations[before + 2] = {
        "row_rect": ScreenRect(left=56, top=164, right=306, bottom=228),
    }
    harness.fake.calls.clear()
    result = harness.worker.execute(command)

    assert result.status in {WorkerStatus.FAILED_SAFE, WorkerStatus.UNCERTAIN}
    assert result.error_code == "selection_visual_attestation_drift"
    assert harness.visual.calls - before == 2
    assert "write-composer" not in harness.fake.calls
    assert "invoke-send" not in harness.fake.calls
    assert result.evidence["selection_attestation"]["retrying"] is False


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
