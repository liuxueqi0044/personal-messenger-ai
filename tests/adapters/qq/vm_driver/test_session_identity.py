import hashlib
import json

import pytest

from messenger_ai.adapters.qq.models import (
    QQConversation,
    QQIdentityBinding,
    QQSelector,
    QQSessionObservedDirectIdentity,
    QQSelectorPack,
    QQWindow,
)
from messenger_ai.adapters.qq.vm_driver import session_identity
from messenger_ai.adapters.qq.vm_driver.session_identity import (
    QQSessionCandidateLocator,
    QQSessionIdentityCertifier,
)
from types import SimpleNamespace


HASH = "a" * 64


def evidence(binding_id: str = "binding-1") -> QQSessionObservedDirectIdentity:
    return QQSessionObservedDirectIdentity(
        binding_id=binding_id,
        conversation_type="direct",
        type_evidence_source="operator_observed_direct",
        client_version="9.9.33.51802",
        selector_pack_version="q1",
        group_marker_probe_complete=True,
        group_marker_count=0,
        process_id=7352,
        window_handle=393974,
        process_started_at_100ns=123,
        vm_environment_fingerprint=HASH,
        selected_row_runtime_id_hash="b" * 64,
        header_digest="c" * 64,
    )


def binding(proof: QQSessionObservedDirectIdentity) -> QQIdentityBinding:
    return QQIdentityBinding(
        hub_conversation_id="conversation-1", contact_id="contact-1",
        account_id="qq-session-account", platform_conversation_id="session-only",
        participant_signature=proof.participant_signature, binding_id=proof.binding_id,
        conversation_type="direct", authorization_scope="all_direct_including_temporary",
    )


def test_session_signature_survives_qq_window_recreation_but_legacy_does_not() -> None:
    first = evidence()
    assert first.participant_signature.startswith("qq-session-observed:")
    assert first.participant_signature == first.model_copy().participant_signature
    recreated = first.model_copy(update={
        "process_id": 8731,
        "window_handle": 984514,
        "process_started_at_100ns": 456,
        "selected_row_runtime_id_hash": "d" * 64,
    })
    assert recreated.participant_signature == first.participant_signature
    assert recreated.legacy_participant_signature != first.legacy_participant_signature
    upgraded = first.model_copy(update={
        "client_version": "9.9.34.1",
        "selector_pack_version": "q2",
        "vm_environment_fingerprint": "d" * 64,
    })
    assert upgraded.participant_signature == first.participant_signature
    assert upgraded.legacy_participant_signature != first.legacy_participant_signature


@pytest.mark.parametrize("field,value", [
    ("binding_id", "binding-2"),
    ("header_digest", "d" * 64),
    ("conversation_type", "group"),
])
def test_session_signature_changes_when_stable_identity_proof_changes(field, value) -> None:
    first = evidence()
    assert first.model_copy(update={field: value}).participant_signature != first.participant_signature


def test_legacy_signature_exactly_reproduces_pre_stability_algorithm() -> None:
    first = evidence()
    legacy = {
        "binding_id": first.binding_id,
        "client_version": first.client_version,
        "conversation_type": first.conversation_type,
        "header_digest": first.header_digest,
        "process_id": first.process_id,
        "process_started_at_100ns": first.process_started_at_100ns,
        "selected_row_runtime_id_hash": first.selected_row_runtime_id_hash,
        "selector_pack_version": first.selector_pack_version,
        "type_evidence_source": first.type_evidence_source,
        "vm_environment_fingerprint": first.vm_environment_fingerprint,
        "window_handle": first.window_handle,
    }
    digest = hashlib.sha256(json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert first.legacy_participant_signature == f"qq-session-observed:{digest}"


def test_locator_uses_registered_runtime_id_and_matching_signature() -> None:
    proof = evidence()
    target = QQConversation(internal_id="runtime:" + "b" * 64, display_name="ignored",
                            participant_signature="uncertified:any", tree_digest="tree")
    other = target.model_copy(update={"internal_id": "runtime:" + "d" * 64})
    locator = QQSessionCandidateLocator((proof,))
    assert locator.locate_candidates(binding(proof), [other, target]) == [target]
    wrong = binding(proof).model_copy(update={"participant_signature": "qq-session-observed:" + "0" * 64})
    assert locator.locate_candidates(wrong, [target]) == []


def pack(proof: QQSessionObservedDirectIdentity) -> QQSelectorPack:
    return QQSelectorPack(client_version=proof.client_version,
        environment_fingerprint=proof.vm_environment_fingerprint,
        selectors=(QQSelector(
            name="composer", control_type="Group",
            class_name_tokens=("ProseMirror", "ExEditor-qq-msg-editor"),
            required_patterns=("TextPattern",),
        ),),
        last_verified_at="2026-09-10T00:00:00Z",
        fixture_suite_version=proof.selector_pack_version)


class Access:
    def __init__(self, classes=()): self.classes = classes
    def _window(self, _window): return object()
    def _descendants(self, _root):
        return [SimpleNamespace(ClassName=value) for value in self.classes]


def candidate(proof: QQSessionObservedDirectIdentity) -> QQConversation:
    return QQConversation(internal_id="runtime:" + proof.selected_row_runtime_id_hash,
                          participant_signature="uncertified:locator", tree_digest="tree")


def cert_window(proof: QQSessionObservedDirectIdentity) -> QQWindow:
    return QQWindow(process_id=proof.process_id, window_handle=proof.window_handle,
                    class_name="Chrome_WidgetWin_1")


def test_certifier_returns_current_registered_proof(monkeypatch) -> None:
    proof = evidence()
    monkeypatch.setattr(session_identity, "_process_started_at_100ns",
                        lambda _pid: proof.process_started_at_100ns)
    monkeypatch.setattr(
        session_identity, "_header_digest",
        lambda _access, _window: proof.header_digest,
    )
    certifier = QQSessionIdentityCertifier(accessibility=Access(), selector_pack=pack(proof), evidence=(proof,))
    assert certifier.certify_current(cert_window(proof), candidate(proof)) == proof


def test_certifier_rejects_process_restart_and_header_drift(monkeypatch) -> None:
    proof = evidence()
    certifier = QQSessionIdentityCertifier(accessibility=Access(), selector_pack=pack(proof), evidence=(proof,))
    monkeypatch.setattr(session_identity, "_process_started_at_100ns",
                        lambda _pid: proof.process_started_at_100ns + 1)
    with pytest.raises(RuntimeError, match="restarted"):
        certifier.certify_current(cert_window(proof), candidate(proof))
    monkeypatch.setattr(session_identity, "_process_started_at_100ns",
                        lambda _pid: proof.process_started_at_100ns)
    monkeypatch.setattr(
        session_identity, "_header_digest",
        lambda _access, _window: "d" * 64,
    )
    with pytest.raises(RuntimeError, match="header"):
        certifier.certify_current(cert_window(proof), candidate(proof))


def test_certifier_rejects_observed_group_marker(monkeypatch) -> None:
    proof = evidence()
    monkeypatch.setattr(session_identity, "_process_started_at_100ns",
                        lambda _pid: proof.process_started_at_100ns)
    certifier = QQSessionIdentityCertifier(accessibility=Access(("group-member-list",)),
        selector_pack=pack(proof), evidence=(proof,))
    with pytest.raises(RuntimeError, match="group_marker"):
        certifier.certify_current(cert_window(proof), candidate(proof))


def test_header_pattern_probe_runs_only_for_structural_header_candidate() -> None:
    bounds = SimpleNamespace(left=0, top=0, right=2560, bottom=1429)
    wrong = [
        SimpleNamespace(
            ControlTypeName=("Button" if index % 2 else "Group"),
            ClassName="unrelated-control",
            Name="unrelated",
            BoundingRectangle=SimpleNamespace(
                left=10, top=10, right=20, bottom=20
            ),
        )
        for index in range(100)
    ]
    header = SimpleNamespace(
        ControlTypeName="Button",
        ClassName="chat-header__contact-name",
        Name="registered contact",
        IsOffscreen=False,
        BoundingRectangle=SimpleNamespace(
            left=328, top=64, right=500, bottom=104
        ),
    )

    class HeaderAccess:
        pattern_probes = 0

        def _window(self, _window):
            return SimpleNamespace(BoundingRectangle=bounds)

        def _descendants(self, _root):
            return [*wrong, header]

        def _select(self, _root, _selector):
            raise AssertionError("header identity must not query composer selectors")

        @staticmethod
        def _property(item, name, default=""):
            return getattr(item, name, default)

        @staticmethod
        def _control_type(value):
            return str(value).lower().removesuffix("control")

        def _has_required_patterns(self, item, patterns):
            assert item is header and patterns == ("invokepattern",)
            self.pattern_probes += 1
            return True

    access = HeaderAccess()
    digest = session_identity._header_digest(
        access, QQWindow(process_id=7, window_handle=9, class_name="QQ"),
    )

    assert len(digest) == 64
    assert access.pattern_probes == 1


def test_header_digest_survives_resize_but_changes_with_contact_title() -> None:
    class HeaderAccess:
        def __init__(self, *, bounds, header_rect, title):
            self.bounds = bounds
            self.header = SimpleNamespace(
                ControlTypeName="Button",
                ClassName="chat-header__contact-name",
                Name=title,
                IsOffscreen=False,
                BoundingRectangle=header_rect,
            )

        def _window(self, _window):
            return SimpleNamespace(BoundingRectangle=self.bounds)

        def _descendants(self, _root):
            return [self.header]

        def _select(self, _root, _selector):
            raise AssertionError("header identity must not query composer selectors")

        @staticmethod
        def _property(item, name, default=""):
            return getattr(item, name, default)

        @staticmethod
        def _control_type(value):
            return str(value).lower().removesuffix("control")

        @staticmethod
        def _has_required_patterns(_item, patterns):
            return patterns == ("invokepattern",)

    def digest(*, width, height, pane_left, title="registered contact"):
        access = HeaderAccess(
            bounds=SimpleNamespace(left=0, top=0, right=width, bottom=height),
            header_rect=SimpleNamespace(
                left=pane_left + 18, top=42,
                right=pane_left + 178, bottom=82,
            ),
            title=title,
        )
        return session_identity._header_digest(
            access, QQWindow(process_id=7, window_handle=9, class_name="QQ"),
        )

    normal = digest(width=820, height=612, pane_left=250)
    maximized = digest(width=2560, height=1429, pane_left=310)

    assert normal == maximized
    assert digest(
        width=2560, height=1429, pane_left=310, title="another contact"
    ) != maximized


def _live_header_reads(monkeypatch, digest):
    reads: list[object] = []
    monkeypatch.setattr(session_identity, "_header_digest",
                        lambda *args: reads.append(args) or digest)
    return reads


def test_try_certify_already_current_returns_unique_registered_proof(monkeypatch) -> None:
    proof = evidence()
    monkeypatch.setattr(session_identity, "_process_started_at_100ns",
                        lambda _pid: proof.process_started_at_100ns)
    _live_header_reads(monkeypatch, proof.header_digest)
    certifier = QQSessionIdentityCertifier(
        accessibility=Access(), selector_pack=pack(proof), evidence=(proof,)
    )
    assert certifier.try_certify_already_current(
        cert_window(proof), candidate(proof)
    ) == proof


def test_try_certify_already_current_returns_none_on_live_header_drift(monkeypatch) -> None:
    proof = evidence()
    monkeypatch.setattr(session_identity, "_process_started_at_100ns",
                        lambda _pid: proof.process_started_at_100ns)
    _live_header_reads(monkeypatch, "d" * 64)
    certifier = QQSessionIdentityCertifier(
        accessibility=Access(), selector_pack=pack(proof), evidence=(proof,)
    )
    assert certifier.try_certify_already_current(
        cert_window(proof), candidate(proof)
    ) is None


def test_try_certify_already_current_returns_none_for_duplicate_header_without_live_read(
    monkeypatch,
) -> None:
    first = evidence("binding-1")
    second = first.model_copy(update={
        "binding_id": "binding-2",
        "selected_row_runtime_id_hash": "e" * 64,
    })
    assert second.header_digest == first.header_digest
    monkeypatch.setattr(session_identity, "_process_started_at_100ns",
                        lambda _pid: first.process_started_at_100ns)
    reads = _live_header_reads(monkeypatch, first.header_digest)
    certifier = QQSessionIdentityCertifier(
        accessibility=Access(), selector_pack=pack(first),
        evidence=(first, second),
    )
    assert certifier.try_certify_already_current(
        cert_window(first), candidate(first)
    ) is None
    assert reads == []


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("group", "group_marker"), ("version", "scope_drift"), ("unregistered", "not_registered")],
)
def test_try_certify_already_current_propagates_non_header_identity_errors(
    monkeypatch, scenario, expected,
) -> None:
    proof = evidence()
    monkeypatch.setattr(session_identity, "_process_started_at_100ns",
                        lambda _pid: proof.process_started_at_100ns)
    # The live header must still match so the error is not mistaken for drift.
    _live_header_reads(monkeypatch, proof.header_digest)
    access = Access(("group-member-list",)) if scenario == "group" else Access()
    selectors = pack(proof)
    if scenario == "version":
        selectors = selectors.model_copy(update={"client_version": "9.9.99.1"})
    certifier = QQSessionIdentityCertifier(
        accessibility=access, selector_pack=selectors, evidence=(proof,)
    )
    target = candidate(proof)
    if scenario == "unregistered":
        target = target.model_copy(update={"internal_id": "runtime:" + "f" * 64})
    with pytest.raises(RuntimeError, match=expected):
        certifier.try_certify_already_current(cert_window(proof), target)
