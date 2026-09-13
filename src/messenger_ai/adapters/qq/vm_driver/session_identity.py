"""Current-process QQ identity checks for operator-observed direct sessions."""
from __future__ import annotations

import ctypes
import hashlib
from ctypes import wintypes

from messenger_ai.adapters.qq.models import (
    QQConversation,
    QQIdentityBinding,
    QQSelectorPack,
    QQSessionObservedDirectIdentity,
    QQWindow,
)


_GROUP_MARKERS = {
    "group-box__toggle", "group-user", "group-notice", "group-member-list",
}


def _process_started_at_100ns(process_id: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
                                         ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
                                         ctypes.POINTER(wintypes.FILETIME))
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000, False, process_id)
    if not handle:
        raise RuntimeError("qq_process_session_unavailable")
    creation, exit_time, kernel, user = (wintypes.FILETIME() for _ in range(4))
    try:
        if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            raise RuntimeError("qq_process_session_unavailable")
    finally:
        kernel32.CloseHandle(handle)
    return (int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime)


def _header_digest(
    accessibility,
    window: QQWindow,
    _legacy_composer_selector: object | None = None,
) -> str:
    """Digest the unique visible chat header without touching the composer."""

    root = accessibility._window(window)
    matches = []
    prop = getattr(accessibility, "_property", lambda item, name, default="": getattr(item, name, default))
    bounds = prop(root, "BoundingRectangle", None)
    if bounds is None:
        raise RuntimeError("header_geometry_invalid")
    root_width, root_height = bounds.right - bounds.left, bounds.bottom - bounds.top
    if root_width <= 0 or root_height <= 0:
        raise RuntimeError("header_geometry_invalid")
    for item in accessibility._descendants(root):
        tokens = set(str(prop(item, "ClassName", "")).split())
        if (accessibility._control_type(prop(item, "ControlTypeName", "")) != "button"
                or "chat-header__contact-name" not in tokens):
            continue
        has_invoke = (
            accessibility._has_required_patterns(item, ("invokepattern",))
            if hasattr(accessibility, "_has_required_patterns")
            else "invokepattern" in accessibility._patterns(item)
        )
        if not has_invoke:
            continue
        rect = prop(item, "BoundingRectangle", None)
        if rect is None:
            continue
        if (bool(prop(item, "IsOffscreen", True))
                or rect.right <= rect.left or rect.bottom <= rect.top
                or rect.left < bounds.left or rect.top < bounds.top
                or rect.right > bounds.right or rect.bottom > bounds.bottom):
            continue
        name = " ".join(str(prop(item, "Name", "")).split())
        if name:
            # Window size, display resolution and maximize state may all change
            # the header's root-relative geometry.  The unique semantic class,
            # button type, InvokePattern, visibility and root containment select
            # the live chat header without querying the message composer.
            matches.append(hashlib.sha256(
                f"ControlType.Button|chat-header__contact-name|{name}".encode()
            ).hexdigest())
    if len(matches) != 1:
        raise RuntimeError("header_not_unique")
    return matches[0]


def observe_selected_identity(*, accessibility, selector_pack: QQSelectorPack,
                              window: QQWindow, binding_id: str,
                              operator_observed_direct: bool) -> QQSessionObservedDirectIdentity:
    """Register only the row currently selected and observed by the operator."""
    if not operator_observed_direct:
        raise ValueError("operator direct observation is required")
    if not accessibility._guest_scope(window):
        raise RuntimeError("session_identity_scope_drift")
    root = accessibility._window(window)
    prop = getattr(accessibility, "_property", lambda item, name, default="": getattr(item, name, default))
    rows = accessibility._select(root, selector_pack.selector("conversations"))
    selected = []
    selected_token = selector_pack.selector("conversations").selected_class_name_token
    for row in rows:
        pattern = None
        try:
            pattern = row.GetPattern(10010)
        except Exception:
            pass
        token_match = bool(selected_token and selected_token in str(prop(row, "ClassName", "")).split())
        if token_match or (pattern is not None and bool(getattr(pattern, "IsSelected", False))):
            selected.append(row)
    if len(selected) != 1:
        raise RuntimeError("selected_conversation_not_unique")
    internal_id = accessibility._conversation_id(selected[0])
    if not internal_id.startswith("runtime:"):
        raise RuntimeError("selected_conversation_runtime_id_missing")
    markers = [item for item in accessibility._descendants(root)
               if _GROUP_MARKERS & set(str(prop(item, "ClassName", "")).split())]
    if markers:
        raise RuntimeError("group_marker_detected")
    return QQSessionObservedDirectIdentity(
        binding_id=binding_id,
        conversation_type="direct",
        type_evidence_source="operator_observed_direct",
        client_version=selector_pack.client_version,
        selector_pack_version=selector_pack.fixture_suite_version,
        group_marker_probe_complete=True,
        group_marker_count=0,
        process_id=window.process_id,
        window_handle=window.window_handle,
        process_started_at_100ns=_process_started_at_100ns(window.process_id),
        vm_environment_fingerprint=selector_pack.environment_fingerprint,
        selected_row_runtime_id_hash=internal_id.removeprefix("runtime:"),
        header_digest=_header_digest(accessibility, window),
    )


class QQSessionCandidateLocator:
    def __init__(self, evidence: tuple[QQSessionObservedDirectIdentity, ...]) -> None:
        self._by_binding = {item.binding_id: item for item in evidence}
        if len(self._by_binding) != len(evidence):
            raise ValueError("session evidence binding_id must be unique")

    def locate_candidates(self, binding: object, visible: list[QQConversation]) -> list[QQConversation]:
        if not isinstance(binding, QQIdentityBinding):
            return []
        proof = self._by_binding.get(binding.binding_id)
        if proof is None or binding.participant_signature != proof.participant_signature:
            return []
        expected = f"runtime:{proof.selected_row_runtime_id_hash}"
        return [item for item in visible if item.internal_id == expected]


class QQSessionIdentityCertifier:
    def __init__(self, *, accessibility, selector_pack: QQSelectorPack,
                 evidence: tuple[QQSessionObservedDirectIdentity, ...]) -> None:
        self._accessibility = accessibility
        self._selectors = selector_pack
        scopes = {
            (item.process_id, item.window_handle, item.process_started_at_100ns)
            for item in evidence
        }
        if len(scopes) != 1:
            raise ValueError("session evidence window scope must be unique")
        self._window_scope = next(iter(scopes))
        self._by_locator = {f"runtime:{item.selected_row_runtime_id_hash}": item for item in evidence}
        if len(self._by_locator) != len(evidence):
            raise ValueError("session evidence row locator must be unique")
        self._header_digest_counts: dict[str, int] = {}
        for item in evidence:
            self._header_digest_counts[item.header_digest] = (
                self._header_digest_counts.get(item.header_digest, 0) + 1
            )

    @property
    def window_scope(self) -> tuple[int, int]:
        return self._window_scope[:2]

    def validate_window(self, window: QQWindow) -> None:
        """Bind startup and every UI action to the bootstrapped QQ process window."""
        process_id, window_handle, process_started_at = self._window_scope
        if window.process_id != process_id or window.window_handle != window_handle:
            raise RuntimeError("session_identity_scope_drift")
        if _process_started_at_100ns(window.process_id) != process_started_at:
            raise RuntimeError("session_identity_process_restarted")

    def certify_current(self, window: QQWindow,
                        candidate: QQConversation) -> QQSessionObservedDirectIdentity:
        proof = self._by_locator.get(candidate.internal_id)
        if proof is None:
            raise RuntimeError("session_identity_not_registered")
        self.validate_window(window)
        if (proof.client_version != self._selectors.client_version
                or proof.selector_pack_version != self._selectors.fixture_suite_version
                or proof.vm_environment_fingerprint != self._selectors.environment_fingerprint):
            raise RuntimeError("session_identity_scope_drift")
        root = self._accessibility._window(window)
        prop = getattr(
            self._accessibility, "_property",
            lambda item, name, default="": getattr(item, name, default),
        )
        markers = [item for item in self._accessibility._descendants(root)
                   if _GROUP_MARKERS & set(str(prop(item, "ClassName", "")).split())]
        if markers:
            raise RuntimeError("group_marker_detected")
        if _header_digest(self._accessibility, window) != proof.header_digest:
            raise RuntimeError("session_identity_header_drift")
        return proof

    def try_certify_already_current(
        self, window: QQWindow, candidate: QQConversation,
    ) -> QQSessionObservedDirectIdentity | None:
        """Prove a target is already current without trusting QQ's row marker.

        QQ's Chromium accessibility provider can omit both SelectionItem and
        the selected class token even for the visibly active row.  A unique,
        registered header digest is an independent current-chat proof.  When
        two registered contacts share that digest, no such inference is safe
        and the caller must use the ordinary selection path.
        """

        proof = self._by_locator.get(candidate.internal_id)
        if proof is None:
            raise RuntimeError("session_identity_not_registered")
        if self._header_digest_counts.get(proof.header_digest) != 1:
            return None
        self.validate_window(window)
        if (
            proof.client_version != self._selectors.client_version
            or proof.selector_pack_version != self._selectors.fixture_suite_version
            or proof.vm_environment_fingerprint
            != self._selectors.environment_fingerprint
        ):
            raise RuntimeError("session_identity_scope_drift")
        if _header_digest(self._accessibility, window) != proof.header_digest:
            return None
        # Re-run the complete proof instead of trusting the preliminary header
        # read.  A concurrent selection change, group marker, or scope drift
        # therefore fails closed before any message or composer access.
        return self.certify_current(window, candidate)
