from __future__ import annotations

"""Concrete Windows UIA boundary.

This module uses UIA ValuePattern when available. In the certified guest only,
TextPattern composition may use guarded Win32 Unicode input; it never uses the
clipboard or string-based SendKeys. Sending still requires the visible button's
UIA InvokePattern.
"""

import binascii
import ctypes
import hashlib
import json
import math
import os
import struct
import subprocess
import time
import zlib
from collections.abc import Iterable
from contextlib import contextmanager
from ctypes import wintypes
from datetime import UTC, datetime
from typing import Any, NamedTuple

from messenger_ai.adapters.qq.models import (
    QQBubble,
    QQConversation,
    QQSelector,
    QQWindow,
)

from .guest_composer import (
    _select_all_delete,
    clear_with_local_selection,
    get_uia_pattern,
    read_composer_text,
    write_with_text_pattern,
)
from .message_decoder import decode_message_region
from .phase_index import UIAPhaseIndex
from .visual_selection import (
    QQ_VM_ROW_PALETTE_PROFILE,
    ConversationRowPaletteProfile,
    RowBorderSample,
    RowPaletteSummary,
    ScreenRect,
    SelectionVisualAttestation,
    VisualRowFrame,
    classify_row_border,
    runtime_id_digest,
    summarize_border_pixels,
)


class UIAUnavailable(RuntimeError):
    pass


_QQ_CONVERSATION_LEGACY_ACTION_UTF8_BYTES = 3
_QQ_CONVERSATION_LEGACY_ACTION_SHA256 = (
    "eea50f4175bf5349265e25bf055c4a4fca9d15fed30d03e60763d1698809d6f6"
)

_INPUT_MOUSE = 0
_MOUSEEVENTF_MOVE = 0x0001
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004
_MOUSEEVENTF_VIRTUALDESK = 0x4000
_MOUSEEVENTF_ABSOLUTE = 0x8000
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79
_GA_ROOT = 2

# A neutral hover point keeps this distance from every conversation row so the
# pointer cannot leave a leftover hover highlight on an adjacent row.  It is
# also clamped to the guest virtual screen: a maximized window reports a
# ``GetWindowRect`` inflated by its invisible resize border, so window-local
# corners can fall off the visible desktop even though ``WindowFromPoint``
# still resolves them to the QQ HWND.
_NEUTRAL_HOVER_MARGIN = 3


class _ConversationRowRef(NamedTuple):
    """One enumerated conversation row bound to its runtime locator and rect."""

    internal_id: str
    item: Any
    rect: ScreenRect


def _check_selection_deadline(deadline: datetime | None) -> None:
    """Fail closed as soon as a bounded selection proof runs out of time."""

    if deadline is None:
        return
    if deadline.tzinfo is None or deadline.utcoffset() is None:
        raise ValueError("selection deadline must be timezone-aware")
    if datetime.now(UTC) >= deadline:
        raise RuntimeError("deadline_expired")


class _MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class _InputUnion(ctypes.Union):
    _fields_ = [("mi", _MouseInput)]


class _Input(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _InputUnion)]


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _RgbQuad(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", ctypes.c_ubyte),
        ("rgbGreen", ctypes.c_ubyte),
        ("rgbRed", ctypes.c_ubyte),
        ("rgbReserved", ctypes.c_ubyte),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", _RgbQuad * 1)]


def _encode_bgra_png(width: int, height: int, bgra: bytes) -> bytes:
    if len(bgra) != width * height * 4:
        raise ValueError("BGRA frame length does not match geometry")
    rgba = bytearray(bgra)
    rgba[0::4] = bgra[2::4]
    rgba[2::4] = bgra[0::4]
    rgba[3::4] = b"\xff" * (width * height)
    stride = width * 4
    scanlines = b"".join(
        b"\x00" + bytes(rgba[offset : offset + stride])
        for offset in range(0, len(rgba), stride)
    )

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            len(payload).to_bytes(4, "big")
            + body
            + (binascii.crc32(body) & 0xFFFFFFFF).to_bytes(4, "big")
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines, level=6))
        + chunk(b"IEND", b"")
    )


class WindowsUIAQQAccessibility:
    """Real QQ NT accessibility transport for a *guest* Windows desktop."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise UIAUnavailable("QQ VM transport can only run on Windows")
        if os.environ.get("PERSONAL_MESSENGER_VM_GUEST") != "1":
            raise UIAUnavailable("refusing UI automation outside a certified VM guest")
        try:
            probe = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_ComputerSystem | Select-Object Manufacturer,Model | ConvertTo-Json -Compress"],
                check=True, capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            machine = json.loads(probe.stdout)
            manufacturer = str(machine.get("Manufacturer", "")).lower()
            model = str(machine.get("Model", "")).lower()
        except Exception as exc:
            raise UIAUnavailable("guest machine identity could not be certified") from exc
        if "virtualbox" not in model or not any(name in manufacturer for name in ("innotek", "oracle")):
            raise UIAUnavailable("refusing UI automation: machine is not a certified VirtualBox guest")
        try:
            import uiautomation as auto  # type: ignore[import-not-found]
        except ImportError as exc:
            raise UIAUnavailable("install personal-messenger-ai[qq-vm] in the guest") from exc
        self._auto = auto
        self._active_phase: UIAPhaseIndex | None = None

    @contextmanager
    def read_phase(self, window: QQWindow):
        """Expose one command-local tree snapshot; never nest or reuse it."""

        if getattr(self, "_active_phase", None) is not None:
            raise RuntimeError("UIA read phases cannot be nested")
        root = self._window_uncached(window)
        phase = UIAPhaseIndex(
            window=window,
            root=root,
            pattern_loader=get_uia_pattern,
        )
        self._active_phase = phase
        try:
            yield phase
        finally:
            phase.close()
            if getattr(self, "_active_phase", None) is phase:
                self._active_phase = None

    def find_main_windows(self, selector: QQSelector) -> list[QQWindow]:
        root = self._auto.GetRootControl()
        # Main windows are direct desktop children. Never breadth-first scan the
        # whole desktop before a QQ root has been identified.
        controls = [item for item in root.GetChildren() if self._matches(item, selector, (), ())]
        result: list[QQWindow] = []
        for control in controls:
            handle = int(getattr(control, "NativeWindowHandle", 0) or 0)
            pid = int(getattr(control, "ProcessId", 0) or 0)
            if handle > 0 and pid > 0:
                result.append(QQWindow(process_id=pid, window_handle=handle, class_name=str(getattr(control, "ClassName", "")), title=str(getattr(control, "Name", ""))))
        return result

    def tree_digest(self, window: QQWindow) -> str:
        control = self._window(window)
        rows = []
        for item in self._descendants(control):
            rows.append("\x1f".join((
                str(self._property(item, "ControlTypeName", "")),
                str(self._property(item, "AutomationId", "")),
                str(self._property(item, "ClassName", "")),
            )))
        return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()

    def list_conversations(self, window: QQWindow, selector: QQSelector) -> list[QQConversation]:
        digest = self.tree_digest(window)
        result = []
        for item in self._select(self._window(window), selector):
            name = str(self._property(item, "Name", ""))
            automation_id = str(self._property(item, "AutomationId", ""))
            class_name = str(self._property(item, "ClassName", ""))
            # internal_id is a current-session locator. participant_signature is
            # compared with a separately certified binding and is never promoted
            # from display_name or row position by this transport.
            internal_id = self._conversation_id(item)
            if not internal_id:
                continue
            signature = (hashlib.sha256(f"{automation_id}|{class_name}".encode()).hexdigest()
                         if automation_id else f"uncertified:{internal_id}")
            result.append(QQConversation(internal_id=internal_id, display_name=name, participant_signature=signature, tree_digest=digest))
        return result

    def select_conversation(self, window: QQWindow, conversation: QQConversation, selector: QQSelector) -> bool:
        """Select one row and report whether this process performed a UIA action."""

        if selector.name != "conversation_item":
            raise UIAUnavailable("conversation selection selector is not certified")
        matches = [item for item in self._select(self._window(window), selector) if self._conversation_id(item) == conversation.internal_id]
        if len(matches) != 1:
            raise UIAUnavailable("conversation is absent or ambiguous")
        target = matches[0]
        selection_pattern = self._pattern(target, "GetSelectionItemPattern", 10010)
        token_selected = bool(selector.selected_class_name_token) and selector.selected_class_name_token in str(self._property(target, "ClassName", "")).split()
        already_selected = bool(selection_pattern is not None and getattr(selection_pattern, "IsSelected", False)) or token_selected
        if not already_selected:
            # The deployed session selector version is also embedded in the
            # registered identity evidence.  Its InvokePattern requirement is
            # retained only as structural row evidence.  The production QQ
            # 9.9.33 selection action is independently certified here from the
            # row's observed LegacyIAccessible default action.
            legacy = self._pattern(
                target, "GetLegacyIAccessiblePattern", 10018
            )
            if legacy is None:
                raise UIAUnavailable(
                    "conversation has no certified legacy selection action"
                )
            default_action = getattr(legacy, "DefaultAction", None)
            if not isinstance(default_action, str) or not default_action:
                default_action = getattr(legacy, "CurrentDefaultAction", None)
            encoded_action = (
                default_action.encode("utf-8")
                if isinstance(default_action, str) else b""
            )
            if (
                len(encoded_action) != _QQ_CONVERSATION_LEGACY_ACTION_UTF8_BYTES
                or hashlib.sha256(encoded_action).hexdigest()
                != _QQ_CONVERSATION_LEGACY_ACTION_SHA256
            ):
                raise UIAUnavailable(
                    "conversation legacy default action is not certified"
                )
            action = getattr(legacy, "DoDefaultAction", None)
            if not callable(action):
                raise UIAUnavailable(
                    "conversation has no callable legacy selection action"
                )
            active_phase = getattr(self, "_active_phase", None)
            if active_phase is not None:
                active_phase.invalidate()
            # From the instant DoDefaultAction begins, its outcome is unknown
            # to this COM process.  QQ can switch the row and still raise from
            # the provider.  Report the attempted mutation in both cases so
            # the worker retires this process and verifies in a fresh one.
            try:
                action()
            except Exception:
                return True
            return True
        if getattr(self, "_active_phase", None) is not None:
            # The worker always opens an independent post-selection phase for
            # confirmation and identity certification, even when no click was
            # needed.  This phase must remain a read-only pre-action snapshot.
            return False
        # Selection is an action boundary. Confirmation belongs to the
        # caller's independently refreshed phase so an asynchronous Chromium
        # update cannot become an immediate false failure. Standalone callers
        # explicitly confirm after this method.
        return False

    def confirm_conversation_selected(self, window: QQWindow,
                                      conversation: QQConversation,
                                      selector: QQSelector) -> None:
        refreshed = [item for item in self._select(self._window(window), selector) if self._conversation_id(item) == conversation.internal_id]
        if len(refreshed) != 1:
            raise UIAUnavailable("conversation selection could not be independently confirmed")
        refreshed_pattern = self._pattern(refreshed[0], "GetSelectionItemPattern", 10010)
        token_selected = bool(selector.selected_class_name_token) and selector.selected_class_name_token in str(self._property(refreshed[0], "ClassName", "")).split()
        if not token_selected and not (refreshed_pattern is not None and bool(getattr(refreshed_pattern, "IsSelected", False))):
            raise UIAUnavailable("conversation selection could not be independently confirmed")

    def is_conversation_selected(
        self, window: QQWindow, conversation: QQConversation, selector: QQSelector
    ) -> bool:
        target = self._visual_row_target(window, conversation, selector)
        pattern = self._pattern(target, "GetSelectionItemPattern", 10010)
        token = selector.selected_class_name_token
        return bool(
            (token and token in str(self._property(target, "ClassName", "")).split())
            or (pattern is not None and bool(getattr(pattern, "IsSelected", False)))
        )

    def capture_conversation_row(
        self, window: QQWindow, conversation: QQConversation, selector: QQSelector
    ) -> VisualRowFrame:
        """Capture only one certified row; never expose the chat/content panel."""

        if getattr(self, "_active_phase", None) is not None:
            raise RuntimeError("visual row capture cannot retain a UIA read phase")
        self.ensure_guest_foreground(window)
        target = self._visual_row_target(window, conversation, selector)
        rect = self._row_screen_rect(target)
        try:
            png_bytes = self._capture_exact_window_row(window, rect)
        except Exception as exc:
            raise UIAUnavailable("conversation row capture failed") from exc
        return VisualRowFrame(
            process_id=window.process_id,
            window_handle=window.window_handle,
            conversation_internal_id=conversation.internal_id,
            rect=rect,
            png_bytes=png_bytes,
        )

    def click_conversation_row(
        self,
        window: QQWindow,
        conversation: QQConversation,
        selector: QQSelector,
        expected_rect: ScreenRect,
    ) -> bool:
        """Click one visually certified row inside the guest and report only attempt."""

        if getattr(self, "_active_phase", None) is not None:
            raise RuntimeError("visual row action cannot retain a UIA read phase")
        self.ensure_guest_foreground(window)
        target = self._visual_row_target(window, conversation, selector)
        current_rect = self._row_screen_rect(target)
        if current_rect != expected_rect:
            raise UIAUnavailable("conversation row geometry changed before visual action")
        if self.is_conversation_selected(window, conversation, selector):
            return False
        x = current_rect.left + current_rect.width // 2
        y = current_rect.top + current_rect.height // 2
        if not self._point_belongs_to_window(window, x, y):
            raise UIAUnavailable("visual click point left the certified QQ window")
        sent = self._send_guest_click(x, y)
        if sent != 3:
            raise UIAUnavailable("visual row mouse input was incomplete")
        return True

    def certify_conversation_selected_visual(
        self,
        window: QQWindow,
        conversation: QQConversation,
        selector: QQSelector,
        profile: ConversationRowPaletteProfile = QQ_VM_ROW_PALETTE_PROFILE,
        *,
        deadline: datetime | None = None,
    ) -> SelectionVisualAttestation:
        """Prove one exact conversation row is the selected row, locally.

        The proof is deterministic, fail-closed and content-free: it samples
        only the border band of already-located rows, requires exactly one
        selected row (the runtime-``internal_id`` target) plus at least two
        unselected control rows, and succeeds only after two consecutive
        identical, fully classified samples.  No vision model, PNG bytes, chat
        text, row text or window title is produced, returned or persisted.
        """

        if not isinstance(profile, ConversationRowPaletteProfile):
            raise TypeError("a conversation row palette profile is required")
        if selector.name != "conversation_item":
            raise UIAUnavailable("conversation selection selector is not certified")
        if getattr(self, "_active_phase", None) is not None:
            raise RuntimeError(
                "visual selection certification cannot retain a UIA read phase"
            )
        _check_selection_deadline(deadline)
        foreground_timeout = profile.foreground_timeout_seconds
        if deadline is not None:
            foreground_timeout = min(
                foreground_timeout,
                max(0.0, (deadline - datetime.now(UTC)).total_seconds()),
            )
            _check_selection_deadline(deadline)
        self.ensure_guest_foreground(
            window, timeout_seconds=foreground_timeout
        )
        _check_selection_deadline(deadline)
        rows = self._visible_conversation_rows(window, selector)
        if not rows:
            raise UIAUnavailable("no visible conversation rows were enumerated")
        target_id = conversation.internal_id
        if sum(1 for ref in rows if ref.internal_id == target_id) != 1:
            raise UIAUnavailable(
                "conversation target is absent or ambiguous in the visible rows"
            )
        bounds = self._window_screen_bounds(window)
        row_rects = {ref.internal_id: ref.rect for ref in rows}
        for ref in rows:
            rect = ref.rect
            if (
                abs(rect.width - profile.row_width) > profile.geometry_tolerance
                or abs(rect.height - profile.row_height) > profile.geometry_tolerance
            ):
                raise UIAUnavailable(
                    "conversation row geometry does not match the certified profile"
                )
            if not (
                bounds[0] <= rect.left < rect.right <= bounds[2]
                and bounds[1] <= rect.top < rect.bottom <= bounds[3]
            ):
                raise UIAUnavailable(
                    "conversation row crop left the certified QQ window"
                )
            if not all(
                self._point_belongs_to_window(window, x, y)
                for x, y in self._row_capture_points(rect)
            ):
                raise UIAUnavailable(
                    "conversation rows are not all inside one certified QQ window"
                )
        neutral = self._neutral_hover_point(window, rows, bounds)
        _check_selection_deadline(deadline)

        previous_key: tuple[object, ...] | None = None
        consecutive = 0
        sample_count = 0
        while True:
            if sample_count >= profile.max_samples:
                raise UIAUnavailable(
                    "conversation selection sampling exhausted its bounded budget"
                )
            _check_selection_deadline(deadline)
            sample_count += 1
            # Clear any hover highlight before every sample: a hovered row is
            # indistinguishable from a selected row by ratio alone.
            self._send_guest_mouse_move(*neutral)
            if profile.hover_settle_seconds > 0:
                time.sleep(profile.hover_settle_seconds)
            _check_selection_deadline(deadline)
            if self._window_screen_bounds(window) != bounds:
                raise UIAUnavailable(
                    "certified QQ window changed during selection certification"
                )
            current = self._visible_conversation_rows(window, selector)
            if [ref.internal_id for ref in current] != [
                ref.internal_id for ref in rows
            ]:
                raise UIAUnavailable(
                    "conversation row set changed during selection certification"
                )
            for ref in current:
                if ref.rect != row_rects[ref.internal_id]:
                    raise UIAUnavailable(
                        "conversation row geometry changed during selection certification"
                    )

            selected_ids: list[str] = []
            unselected_ids: list[str] = []
            summaries: dict[str, RowBorderSample] = {}
            key_parts: list[tuple[object, ...]] = []
            for ref in current:
                sample = self._sample_row_border(window, ref, profile)
                state = classify_row_border(sample, profile)
                if state == "hover":
                    raise UIAUnavailable(
                        "a conversation row reports the hover palette"
                    )
                if state == "unknown":
                    raise UIAUnavailable(
                        "a conversation row border palette is unrecognized"
                    )
                if state == "selected":
                    selected_ids.append(ref.internal_id)
                else:
                    unselected_ids.append(ref.internal_id)
                summaries[ref.internal_id] = sample
                key_parts.append(
                    (
                        ref.internal_id,
                        state,
                        sample.pixel_count,
                        sample.dominant_rgb,
                        sample.dominant_count,
                        sample.unique_count,
                    )
                )

            if len(selected_ids) > 1:
                raise UIAUnavailable(
                    "more than one conversation row reports the selected palette"
                )
            if not selected_ids:
                # The asynchronous row switch has not committed yet; keep the
                # bounded poll running until it settles or the budget expires.
                previous_key, consecutive = None, 0
                time.sleep(profile.poll_interval_seconds)
                continue
            if selected_ids[0] != target_id:
                raise UIAUnavailable("a different conversation row is selected")
            if len(unselected_ids) < profile.min_unselected_control_rows:
                raise UIAUnavailable(
                    "too few unselected control rows to certify selection"
                )

            key = tuple(key_parts)
            consecutive = consecutive + 1 if key == previous_key else 1
            previous_key = key
            if consecutive >= profile.stable_samples:
                return SelectionVisualAttestation(
                    profile_id=profile.profile_id,
                    client_version=profile.client_version,
                    selector_pack_version=profile.selector_pack_version,
                    environment_fingerprint=profile.environment_fingerprint,
                    process_id=window.process_id,
                    window_handle=window.window_handle,
                    target_runtime_id_digest=runtime_id_digest(target_id),
                    row_rect=row_rects[target_id],
                    sample_count=sample_count,
                    stable_sample_count=consecutive,
                    unselected_control_count=len(unselected_ids),
                    selected=RowPaletteSummary.from_sample(summaries[target_id]),
                    unselected=RowPaletteSummary.from_sample(
                        summaries[unselected_ids[0]]
                    ),
                )
            time.sleep(profile.poll_interval_seconds)

    def _visible_conversation_rows(
        self, window: QQWindow, selector: QQSelector
    ) -> list[_ConversationRowRef]:
        """Re-enumerate every visible conversation row with its runtime locator."""

        if selector.name != "conversation_item":
            raise UIAUnavailable("conversation selection selector is not certified")
        refs: list[_ConversationRowRef] = []
        for item in self._select(self._window(window), selector):
            if bool(self._property(item, "IsOffscreen", True)):
                continue
            internal_id = self._conversation_id(item)
            if not internal_id:
                continue
            refs.append(_ConversationRowRef(internal_id, item, self._row_screen_rect(item)))
        return refs

    def _sample_row_border(
        self,
        window: QQWindow,
        ref: _ConversationRowRef,
        profile: ConversationRowPaletteProfile,
    ) -> RowBorderSample:
        """Capture one row locally and reduce it to content-free palette stats."""

        rect = ref.rect
        try:
            bgra = self._capture_row_bgra(window, rect)
            return summarize_border_pixels(
                bgra,
                width=rect.width,
                height=rect.height,
                inset=profile.border_inset,
            )
        except UIAUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 - capture must fail closed
            raise UIAUnavailable("conversation row palette sampling failed") from exc

    def _capture_row_bgra(self, window: QQWindow, rect: ScreenRect) -> bytes:
        return self._capture_exact_window_row_bgra(window, rect)

    def _window_screen_bounds(self, window: QQWindow) -> tuple[int, int, int, int]:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(_Rect))
        user32.GetWindowRect.restype = wintypes.BOOL
        window_rect = _Rect()
        if not user32.GetWindowRect(window.window_handle, ctypes.byref(window_rect)):
            raise UIAUnavailable("certified QQ window geometry is unavailable")
        return (
            int(window_rect.left),
            int(window_rect.top),
            int(window_rect.right),
            int(window_rect.bottom),
        )

    def _neutral_hover_point(
        self,
        window: QQWindow,
        rows: list[_ConversationRowRef],
        bounds: tuple[int, int, int, int],
    ) -> tuple[int, int]:
        """Pick a deterministic same-HWND point that hovers no conversation row.

        The candidate band is the certified QQ window intersected with the
        guest virtual screen.  Clamping here is what keeps the point inside
        the exact-HWND virtual-screen guard that ``_send_guest_mouse_move``
        enforces at send time; without it a maximized window's invisible
        resize border pushes every corner candidate off the visible desktop.
        Any point that cannot satisfy both constraints fails closed.
        """

        vx, vy, width, height = self._virtual_screen_metrics()
        if width <= 1 or height <= 1:
            raise UIAUnavailable(
                "no neutral hover point is available inside the certified QQ window"
            )
        window_left, window_top, window_right, window_bottom = bounds
        left = max(window_left, vx)
        top = max(window_top, vy)
        right = min(window_right, vx + width)
        bottom = min(window_bottom, vy + height)
        inner_left, inner_top = left + 2, top + 2
        inner_right, inner_bottom = right - 3, bottom - 3
        if inner_right <= inner_left or inner_bottom <= inner_top:
            raise UIAUnavailable(
                "certified QQ window is too small for a neutral hover point"
            )
        blocked = [
            (
                ref.rect.left - _NEUTRAL_HOVER_MARGIN,
                ref.rect.top - _NEUTRAL_HOVER_MARGIN,
                ref.rect.right + _NEUTRAL_HOVER_MARGIN,
                ref.rect.bottom + _NEUTRAL_HOVER_MARGIN,
            )
            for ref in rows
        ]
        middle_x = (inner_left + inner_right) // 2
        candidates = (
            (inner_right, inner_top),
            (inner_right, inner_bottom),
            (inner_left, inner_top),
            (inner_left, inner_bottom),
            (middle_x, inner_top),
            (middle_x, inner_bottom),
        )
        for x, y in candidates:
            if not (vx <= x < vx + width and vy <= y < vy + height):
                continue
            if any(
                block[0] <= x < block[2] and block[1] <= y < block[3]
                for block in blocked
            ):
                continue
            if self._point_belongs_to_window(window, x, y):
                return (x, y)
        raise UIAUnavailable(
            "no neutral hover point is available inside the certified QQ window"
        )

    @staticmethod
    def _virtual_screen_metrics() -> tuple[int, int, int, int]:
        """Return the guest virtual screen as ``(left, top, width, height)``."""

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        user32.GetSystemMetrics.restype = ctypes.c_int
        return (
            int(user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)),
            int(user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)),
            int(user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)),
            int(user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)),
        )

    @staticmethod
    def _send_guest_mouse_move(x: int, y: int) -> None:
        """Move the guest pointer without clicking, to clear any row hover."""

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(_Input),
            ctypes.c_int,
        )
        user32.SendInput.restype = wintypes.UINT
        vx, vy, width, height = WindowsUIAQQAccessibility._virtual_screen_metrics()
        if width <= 1 or height <= 1 or not (
            vx <= x < vx + width and vy <= y < vy + height
        ):
            raise UIAUnavailable(
                "neutral hover point is outside the guest virtual screen"
            )
        nx = round((x - vx) * 65535 / (width - 1))
        ny = round((y - vy) * 65535 / (height - 1))
        events = (_Input * 1)(
            _Input(
                _INPUT_MOUSE,
                _InputUnion(mi=_MouseInput(
                    nx,
                    ny,
                    0,
                    _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                    0,
                    0,
                )),
            ),
        )
        if int(user32.SendInput(1, events, ctypes.sizeof(_Input))) != 1:
            raise UIAUnavailable("hover-clearing mouse move was incomplete")

    def _visual_row_target(
        self, window: QQWindow, conversation: QQConversation, selector: QQSelector
    ) -> Any:
        if selector.name != "conversation_item":
            raise UIAUnavailable("conversation selection selector is not certified")
        matches = [
            item
            for item in self._select(self._window(window), selector)
            if self._conversation_id(item) == conversation.internal_id
        ]
        if len(matches) != 1:
            raise UIAUnavailable("conversation is absent or ambiguous")
        target = matches[0]
        if not bool(self._property(target, "IsEnabled", False)):
            raise UIAUnavailable("conversation row is disabled")
        if bool(self._property(target, "IsOffscreen", True)):
            raise UIAUnavailable("conversation row is offscreen")
        return target

    def _row_screen_rect(self, target: Any) -> ScreenRect:
        rect = self._property(target, "BoundingRectangle", None)
        if rect is None:
            raise UIAUnavailable("conversation row geometry is unavailable")
        try:
            values = [float(getattr(rect, name)) for name in ("left", "top", "right", "bottom")]
        except Exception as exc:
            raise UIAUnavailable("conversation row geometry is unavailable") from exc
        if not all(math.isfinite(value) for value in values):
            raise UIAUnavailable("conversation row geometry is unavailable")
        left, top, right, bottom = values
        try:
            return ScreenRect(
                left=math.floor(left),
                top=math.floor(top),
                right=math.ceil(right),
                bottom=math.ceil(bottom),
            )
        except ValueError as exc:
            raise UIAUnavailable("conversation row geometry is outside visual bounds") from exc

    @staticmethod
    def _point_belongs_to_window(window: QQWindow, x: int, y: int) -> bool:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.WindowFromPoint.argtypes = (_Point,)
        user32.WindowFromPoint.restype = wintypes.HWND
        user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        )
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        hit = user32.WindowFromPoint(_Point(x, y))
        root = user32.GetAncestor(hit, _GA_ROOT) if hit else 0
        pid = wintypes.DWORD()
        if root:
            user32.GetWindowThreadProcessId(root, ctypes.byref(pid))
        return int(root or 0) == window.window_handle and pid.value == window.process_id

    @staticmethod
    def _send_guest_click(x: int, y: int) -> int:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.SendInput.argtypes = (
            wintypes.UINT,
            ctypes.POINTER(_Input),
            ctypes.c_int,
        )
        user32.SendInput.restype = wintypes.UINT
        vx = user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        vy = user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        width = user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN)
        height = user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN)
        if width <= 1 or height <= 1 or not (vx <= x < vx + width and vy <= y < vy + height):
            raise UIAUnavailable("visual click point is outside the guest virtual screen")
        nx = round((x - vx) * 65535 / (width - 1))
        ny = round((y - vy) * 65535 / (height - 1))
        events = (_Input * 3)(
            _Input(
                _INPUT_MOUSE,
                _InputUnion(mi=_MouseInput(
                    nx,
                    ny,
                    0,
                    _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                    0,
                    0,
                )),
            ),
            _Input(
                _INPUT_MOUSE,
                _InputUnion(mi=_MouseInput(0, 0, 0, _MOUSEEVENTF_LEFTDOWN, 0, 0)),
            ),
            _Input(
                _INPUT_MOUSE,
                _InputUnion(mi=_MouseInput(0, 0, 0, _MOUSEEVENTF_LEFTUP, 0, 0)),
            ),
        )
        return int(user32.SendInput(3, events, ctypes.sizeof(_Input)))

    @staticmethod
    def _row_capture_points(rect: ScreenRect) -> tuple[tuple[int, int], ...]:
        """Return bounded screen points that must all belong to the QQ HWND."""

        center_x = rect.left + rect.width // 2
        center_y = rect.top + rect.height // 2
        return (
            (rect.left, rect.top),
            (rect.right - 1, rect.top),
            (center_x, center_y),
            (rect.left, rect.bottom - 1),
            (rect.right - 1, rect.bottom - 1),
        )

    @staticmethod
    def _capture_exact_window_row(window: QQWindow, rect: ScreenRect) -> bytes:
        """Capture one visible, exact-HWND row and encode PNG without dependencies."""

        bgra = WindowsUIAQQAccessibility._capture_exact_window_row_bgra(window, rect)
        return _encode_bgra_png(rect.width, rect.height, bgra)

    @staticmethod
    def _capture_exact_window_row_bgra(window: QQWindow, rect: ScreenRect) -> bytes:
        """Capture one visible, exact-HWND row as raw top-down BGRA pixels."""

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(_Rect))
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetDC.argtypes = (wintypes.HWND,)
        user32.GetDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
        user32.ReleaseDC.restype = ctypes.c_int
        gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateDIBSection.argtypes = (
            wintypes.HDC,
            ctypes.POINTER(_BitmapInfo),
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.HANDLE,
            wintypes.DWORD,
        )
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.BitBlt.argtypes = (
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HDC,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        )
        gdi32.BitBlt.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
        gdi32.DeleteObject.restype = wintypes.BOOL
        gdi32.DeleteDC.argtypes = (wintypes.HDC,)
        gdi32.DeleteDC.restype = wintypes.BOOL

        window_rect = _Rect()
        if not user32.GetWindowRect(window.window_handle, ctypes.byref(window_rect)):
            raise UIAUnavailable("certified QQ window geometry is unavailable")
        if not (
            window_rect.left <= rect.left < rect.right <= window_rect.right
            and window_rect.top <= rect.top < rect.bottom <= window_rect.bottom
        ):
            raise UIAUnavailable("conversation row crop left the certified QQ window")
        if not all(
            WindowsUIAQQAccessibility._point_belongs_to_window(window, x, y)
            for x, y in WindowsUIAQQAccessibility._row_capture_points(rect)
        ):
            raise UIAUnavailable("conversation row is not fully visible in the certified QQ window")

        # Chromium-backed QQ returns an all-black surface from GetWindowDC.
        # The foreground and five-point HWND checks above make a screen-DC copy
        # safe while retaining the one-row privacy boundary.
        source_dc = user32.GetDC(0)
        memory_dc = gdi32.CreateCompatibleDC(source_dc) if source_dc else 0
        bitmap = old_bitmap = 0
        try:
            if not source_dc or not memory_dc:
                raise UIAUnavailable("exact QQ window capture context is unavailable")
            info = _BitmapInfo()
            info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
            info.bmiHeader.biWidth = rect.width
            info.bmiHeader.biHeight = -rect.height  # top-down BGRA pixels
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            bits = ctypes.c_void_p()
            bitmap = gdi32.CreateDIBSection(
                memory_dc,
                ctypes.byref(info),
                0,
                ctypes.byref(bits),
                0,
                0,
            )
            if not bitmap or not bits.value:
                raise UIAUnavailable("conversation row bitmap allocation failed")
            old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
            if not old_bitmap:
                raise UIAUnavailable("conversation row bitmap selection failed")
            source_x = rect.left
            source_y = rect.top
            if not gdi32.BitBlt(
                memory_dc,
                0,
                0,
                rect.width,
                rect.height,
                source_dc,
                source_x,
                source_y,
                0x00CC0020 | 0x40000000,  # SRCCOPY | CAPTUREBLT
            ):
                raise UIAUnavailable("conversation row exact-HWND capture failed")
            bgra = ctypes.string_at(bits, rect.width * rect.height * 4)
            return bgra
        finally:
            if old_bitmap:
                gdi32.SelectObject(memory_dc, old_bitmap)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            if source_dc:
                user32.ReleaseDC(0, source_dc)


    def write_composer(self, window: QQWindow, text: str, selector: QQSelector) -> None:
        matches = self._select(self._window(window), selector)
        if len(matches) != 1:
            raise UIAUnavailable("composer is absent or ambiguous")
        control = matches[0]
        pattern = self._pattern(control, "GetValuePattern", 10002)
        if pattern is not None and not bool(getattr(pattern, "IsReadOnly", False)):
            pattern.SetValue(text)
        else:
            if self._pattern(control, "GetTextPattern", 10014) is None:
                raise UIAUnavailable("composer has no writable ValuePattern or readable TextPattern")
            write_with_text_pattern(control, text, scope_guard=lambda: self._guest_scope(window),
                                    focus_guard=lambda target: self._composer_focused(target, window))
        active_phase = getattr(self, "_active_phase", None)
        if active_phase is not None:
            active_phase.invalidate()

    def read_composer(self, window: QQWindow, selector: QQSelector) -> str:
        matches = self._select(self._window(window), selector)
        if len(matches) != 1:
            raise UIAUnavailable("composer is absent or ambiguous")
        return read_composer_text(matches[0])

    def clear_composer(self, window: QQWindow, expected_text: str, selector: QQSelector) -> None:
        matches = self._select(self._window(window), selector)
        if len(matches) != 1: raise UIAUnavailable("composer is absent or ambiguous")
        clear_with_local_selection(matches[0], clear_action=_select_all_delete,
            expected_text=expected_text, scope_guard=lambda: self._guest_scope(window),
            focus_guard=lambda target: self._composer_focused(target, window))
        active_phase = getattr(self, "_active_phase", None)
        if active_phase is not None:
            active_phase.invalidate()

    def invoke_send(self, window: QQWindow, selector: QQSelector) -> None:
        matches = self._select(self._window(window), selector)
        if len(matches) != 1:
            raise UIAUnavailable("send button is absent or ambiguous")
        pattern = self._pattern(matches[0], "GetInvokePattern", 10000)
        if pattern is None:
            raise UIAUnavailable("send button has no InvokePattern")
        pattern.Invoke()
        active_phase = getattr(self, "_active_phase", None)
        if active_phase is not None:
            active_phase.invalidate()

    def list_bubbles(self, window: QQWindow, selector: QQSelector) -> list[QQBubble]:
        digest = self.tree_digest(window)
        regions = self._select(self._window(window), selector)
        if len(regions) != 1:
            raise UIAUnavailable("message region is absent or ambiguous")
        return [QQBubble(conversation_internal_id="visible-current-conversation",
            message_key=item.message_key, direction=item.direction, text=item.text,
            observed_at=datetime.now(UTC), tree_digest=digest)
            for item in decode_message_region(regions[0])]

    def _window(self, window: QQWindow) -> Any:
        phase = getattr(self, "_active_phase", None)
        if phase is not None and phase.window == window:
            if not phase.active:
                raise RuntimeError("UIA read phase is no longer valid")
            return phase.root
        return self._window_uncached(window)

    def _window_uncached(self, window: QQWindow) -> Any:
        control = self._auto.ControlFromHandle(window.window_handle)
        if control is None or int(getattr(control, "ProcessId", 0) or 0) != window.process_id:
            raise UIAUnavailable("QQ window no longer matches the worker target")
        return control

    def ensure_guest_foreground(self, window: QQWindow, *, timeout_seconds: float = 2.0) -> None:
        """Activate the exact QQ HWND inside the dedicated guest desktop."""

        if getattr(self, "_active_phase", None) is not None:
            raise RuntimeError("QQ foreground cannot change during a UIA read phase")
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        )
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = ()
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.restype = wintypes.BOOL
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window.window_handle, ctypes.byref(pid))
        if (pid.value != window.process_id
                or not bool(user32.IsWindowVisible(window.window_handle))
                or bool(user32.IsIconic(window.window_handle))):
            raise UIAUnavailable("QQ window no longer matches the worker target")
        if int(user32.GetForegroundWindow() or 0) == window.window_handle:
            return
        user32.SetForegroundWindow(window.window_handle)
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while time.monotonic() < deadline:
            user32.GetWindowThreadProcessId(window.window_handle, ctypes.byref(pid))
            if (pid.value == window.process_id
                    and int(user32.GetForegroundWindow() or 0) == window.window_handle):
                return
            time.sleep(0.025)
        raise UIAUnavailable("QQ foreground request timed out")

    def _guest_scope(self, window: QQWindow) -> bool:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindowVisible.argtypes = (wintypes.HWND,); user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = (wintypes.HWND,); user32.IsIconic.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = (); user32.GetForegroundWindow.restype = wintypes.HWND
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window.window_handle, ctypes.byref(pid))
        return (pid.value == window.process_id and bool(user32.IsWindowVisible(window.window_handle))
                and not bool(user32.IsIconic(window.window_handle))
                and int(user32.GetForegroundWindow() or 0) == window.window_handle)

    def _composer_focused(self, target: Any, window: QQWindow) -> bool:
        focused = self._auto.GetFocusedControl()
        get_target_id = getattr(target, "GetRuntimeId", None)
        target_id = tuple(get_target_id() or ()) if callable(get_target_id) else ()
        for _ in range(16):
            if focused is None: return False
            if int(getattr(focused, "ProcessId", 0) or 0) != window.process_id: return False
            get_id = getattr(focused, "GetRuntimeId", None)
            focused_id = tuple(get_id() or ()) if callable(get_id) else ()
            if focused == target or (target_id and focused_id == target_id): return True
            parent = getattr(focused, "GetParentControl", None)
            focused = parent() if callable(parent) else None
        return False

    def _select(self, root: Any, selector: QQSelector) -> list[Any]:
        return [item for item, automation_ancestors, type_ancestors in self._walk(root)
                if self._matches(item, selector, automation_ancestors, type_ancestors)]

    def _descendants(self, root: Any) -> Iterable[Any]:
        phase = getattr(self, "_active_phase", None)
        if phase is not None and phase.root is root:
            for node in phase.nodes():
                yield node.control
            return
        for item, _automation_ancestors, _type_ancestors in self._walk(root):
            yield item

    def _walk(self, root: Any) -> Iterable[tuple[Any, tuple[str, ...], tuple[str, ...]]]:
        phase = getattr(self, "_active_phase", None)
        if phase is not None and phase.root is root:
            for node in phase.nodes():
                yield node.control, node.automation_ancestors, node.type_ancestors
            return
        queue = [(item, (), ()) for item in root.GetChildren()]
        while queue:
            item, automation_ancestors, type_ancestors = queue.pop(0)
            yield item, automation_ancestors, type_ancestors
            item_id = str(self._property(item, "AutomationId", ""))
            item_type = str(self._property(item, "ControlTypeName", ""))
            queue.extend((child,
                          automation_ancestors + ((item_id,) if item_id else ()),
                          type_ancestors + ((item_type.lower(),) if item_type else ()))
                         for child in item.GetChildren())

    def _conversation_id(self, item: Any) -> str:
        automation_id = str(self._property(item, "AutomationId", ""))
        if automation_id:
            return automation_id
        phase = getattr(self, "_active_phase", None)
        if phase is not None:
            runtime_id = phase.call(item, "GetRuntimeId", None)
        else:
            get_runtime_id = getattr(item, "GetRuntimeId", None)
            runtime_id = get_runtime_id() if callable(get_runtime_id) else None
        if runtime_id:
            return "runtime:" + hashlib.sha256(repr(tuple(runtime_id)).encode()).hexdigest()
        return ""

    def _matches(self, item: Any, selector: QQSelector, automation_ancestors: tuple[str, ...], type_ancestors: tuple[str, ...]) -> bool:
        if self._control_type(self._property(item, "ControlTypeName", "")) != self._control_type(selector.control_type):
            return False
        if selector.automation_id and str(self._property(item, "AutomationId", "")) != selector.automation_id:
            return False
        class_name = str(self._property(item, "ClassName", ""))
        if selector.class_name and class_name != selector.class_name:
            return False
        if selector.class_name_tokens and not set(selector.class_name_tokens).issubset(class_name.split()):
            return False
        if selector.required_patterns and not self._has_required_patterns(item, selector.required_patterns):
            return False
        if selector.ancestor_automation_ids and tuple(selector.ancestor_automation_ids) != automation_ancestors[-len(selector.ancestor_automation_ids):]:
            return False
        wanted_types = tuple(self._control_type(value) for value in selector.ancestor_control_types)
        actual_types = tuple(self._control_type(value) for value in type_ancestors)
        return not wanted_types or wanted_types == actual_types[-len(wanted_types):]

    @staticmethod
    def _control_type(value: object) -> str:
        normalized = str(value).strip().lower().replace("controltype.", "")
        return normalized[:-7] if normalized.endswith("control") else normalized

    _PATTERN_GETTERS = {
            "selectionitempattern": ("GetSelectionItemPattern", 10010),
            "selectionpattern": ("GetSelectionPattern", 10001),
            "invokepattern": ("GetInvokePattern", 10000),
            "valuepattern": ("GetValuePattern", 10002),
            "textpattern": ("GetTextPattern", 10014),
            "scrollpattern": ("GetScrollPattern", 10004),
            "scrollitempattern": ("GetScrollItemPattern", 10017),
            "legacyiaccessiblepattern": ("GetLegacyIAccessiblePattern", 10018),
    }

    def _property(self, item: Any, name: str, default: object = "") -> object:
        phase = getattr(self, "_active_phase", None)
        if phase is not None:
            return phase.property(item, name, default)
        return getattr(item, name, default)

    def _pattern(self, item: Any, getter_name: str, pattern_id: int) -> object | None:
        phase = getattr(self, "_active_phase", None)
        if phase is not None:
            return phase.pattern(item, getter_name, pattern_id)
        return get_uia_pattern(item, getter_name, pattern_id)

    def _has_required_patterns(self, item: Any,
                               names: tuple[str, ...] | list[str]) -> bool:
        for raw_name in names:
            name = raw_name.lower()
            getter = self._PATTERN_GETTERS.get(name)
            if getter is None or self._pattern(item, *getter) is None:
                return False
        return True

    def _patterns(self, item: Any) -> set[str]:
        supported: set[str] = set()
        for name, (getter_name, pattern_id) in self._PATTERN_GETTERS.items():
            try:
                if self._pattern(item, getter_name, pattern_id) is not None:
                    supported.add(name)
            except Exception:
                continue
        return supported
