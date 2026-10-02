"""QQ guest navigation through fresh UIA and exact-HWND rendering only.

Guard publication is a NEW trusted-parent interface, not yet runtime assembly.
PrintWindow is intentionally a fail-closed capture candidate: Chromium builds
that return a black/empty bitmap need a separately verified capture strategy.
There is no desktop BitBlt fallback and no message/composer input capability.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import math
import os
import time
from ctypes import wintypes
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import Field, field_validator

from messenger_ai.adapters.qq.models import QQSelector, QQWindow
from messenger_ai.adapters.qq.vm_driver.current_chat_structure import current_chat_structure_digest
from messenger_ai.adapters.qq.vm_driver.guest_composer import get_uia_pattern, read_composer_text
from messenger_ai.adapters.qq.vm_driver.session_identity import _GROUP_MARKERS
from messenger_ai.adapters.qq.vm_driver.transport import (
    UIAUnavailable, WindowsUIAQQAccessibility, _BitmapInfo, _BitmapInfoHeader, _encode_bgra_png,
)

from .contracts import (
    ContactTarget, NavigationFrame, NavigationModel, NavigationRect, NavigationRegion, _aware,
)
from .desktop import NavigationDesktopError, NavigationDesktopScope, ScopedDesktopOperator
from .identity import CurrentChatWitness
from .worker_process import NavigationWorkerCommand, NavigationWorkerProcess, NavigationWorkerResult


class NavigationGuardState(NavigationModel):
    """Atomic parent-owned snapshot of actual runtime ownership/control.

    A standalone navigation test owner may publish this without starting the
    bot. A production publisher must consult the runtime's real pause, lease,
    binding, and in-flight obligations; populating defaults is insufficient.
    """
    schema_version: Literal["qq_navigation_guard_v2"] = "qq_navigation_guard_v2"
    target: ContactTarget
    run_id: str = Field(min_length=1)
    session_epoch: str = Field(min_length=1)
    surface_epoch: str = Field(min_length=1)
    worker_epoch: str = Field(min_length=1)
    observation_epoch: str | None = Field(default=None, min_length=1)
    desktop_lease_id: str = Field(min_length=1)
    lease_expires_at: datetime
    control_revision: int = Field(ge=0, strict=True)
    process_id: int = Field(gt=0, strict=True)
    window_handle: int = Field(gt=0, strict=True)
    process_started_at_100ns: int = Field(gt=0, strict=True)
    paused: bool = Field(strict=True)
    has_owned_draft: bool = Field(strict=True)
    has_commit_obligation: bool = Field(strict=True)
    published_at: datetime
    _times = field_validator("lease_expires_at", "published_at")(_aware)


class WindowsNavigationConfig(NavigationModel):
    guard_state_path: str = Field(min_length=1)
    window: QQWindow
    expected_process_started_at_100ns: int = Field(gt=0, strict=True)
    expected_run_id: str = Field(min_length=1)
    expected_worker_epoch: str = Field(min_length=1)
    list_selector: QQSelector
    row_selector: QQSelector
    name_selector: QQSelector
    name_container_selector: QQSelector | None = None
    search_selector: QQSelector
    search_container_selector: QQSelector | None = None
    header_selector: QQSelector
    composer_selector: QQSelector
    message_selector: QQSelector


def selected_runtime_token(runtime_id: tuple[int, ...] | list[int]) -> str:
    if not runtime_id or any(isinstance(value, bool) or not isinstance(value, int) for value in runtime_id):
        raise NavigationDesktopError("navigation_selected_token_missing")
    return hashlib.sha256(".".join(str(value) for value in runtime_id).encode("utf-8")).hexdigest()


def mask_bgra(width: int, height: int, pixels: bytes, visible: tuple[NavigationRect, ...]) -> bytes:
    """Whitelist only certified label/search pixels; every other pixel is blank."""
    if len(pixels) != width * height * 4:
        raise NavigationDesktopError("navigation_capture_geometry_invalid")
    output = bytearray(b"\x20\x20\x20\xff" * (width * height))
    for rect in visible:
        if rect.right > width or rect.bottom > height:
            raise NavigationDesktopError("navigation_privacy_region_invalid")
        for y in range(rect.top, rect.bottom):
            start, end = (y * width + rect.left) * 4, (y * width + rect.right) * 4
            output[start:end] = pixels[start:end]
    return bytes(output)


def _digest_pixels(pixels: bytes, width: int, region: NavigationRect) -> str:
    payload = b"".join(pixels[(y * width + region.left) * 4:(y * width + region.right) * 4]
                       for y in range(region.top, region.bottom))
    return hashlib.sha256(payload).hexdigest()


def foreground_belongs_to_exact_main(*, foreground: int, foreground_pid: int,
    foreground_root: int, main_pid: int, window: QQWindow, visible: bool, iconic: bool) -> bool:
    """Accept a renderer child only under the exact main root, never ROOTOWNER."""
    return bool(foreground and foreground_pid == window.process_id and main_pid == window.process_id
                and foreground_root == window.window_handle and visible and not iconic)


class _NativeWindowsSurface:
    """Local synchronous primitives; worker hard limits contain native hangs."""
    def __init__(self, transport):
        self.transport = transport
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user.GetDpiForWindow.argtypes = (wintypes.HWND,)
        self.user.GetDpiForWindow.restype = wintypes.UINT
        self.user.GetForegroundWindow.argtypes = ()
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user.IsWindowVisible.restype = wintypes.BOOL
        self.user.IsIconic.argtypes = (wintypes.HWND,)
        self.user.IsIconic.restype = wintypes.BOOL
        self.user.IsZoomed.argtypes = (wintypes.HWND,)
        self.user.IsZoomed.restype = wintypes.BOOL
        self.kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
        self.kernel.GetProcessTimes.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.kernel.CloseHandle.restype = wintypes.BOOL

    def snapshot(self, window):
        foreground = int(self.user.GetForegroundWindow() or 0)
        fg_pid, main_pid = wintypes.DWORD(), wintypes.DWORD()
        self.user.GetWindowThreadProcessId(foreground, ctypes.byref(fg_pid))
        self.user.GetWindowThreadProcessId(window.window_handle, ctypes.byref(main_pid))
        root = int(self.user.GetAncestor(foreground, 2) or 0)
        if not foreground_belongs_to_exact_main(foreground=foreground, foreground_pid=fg_pid.value,
            foreground_root=root, main_pid=main_pid.value, window=window,
            visible=bool(self.user.IsWindowVisible(window.window_handle)), iconic=bool(self.user.IsIconic(window.window_handle))):
            raise NavigationDesktopError("navigation_qq_not_foreground")
        if not self.user.IsZoomed(window.window_handle):
            raise NavigationDesktopError("navigation_qq_not_maximized")
        handle = self.kernel.OpenProcess(0x1000, False, window.process_id)
        if not handle:
            raise NavigationDesktopError("navigation_process_lifetime_unavailable")
        values = [wintypes.FILETIME() for _ in range(4)]
        try:
            if not self.kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in values)):
                raise NavigationDesktopError("navigation_process_lifetime_unavailable")
        finally:
            self.kernel.CloseHandle(handle)
        start = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
        bounds = self.transport._window_screen_bounds(window)
        screen = self.transport._virtual_screen_metrics()
        dpi = int(self.user.GetDpiForWindow(window.window_handle))
        if dpi <= 0:
            raise NavigationDesktopError("navigation_dpi_unavailable")
        return bounds, screen, dpi / 96, start

    def capture(self, window, bounds):
        left, top, right, bottom = bounds
        width, height = right - left, bottom - top
        if not 0 < width <= 8192 or not 0 < height <= 8192:
            raise NavigationDesktopError("navigation_window_geometry_invalid")
        self.user.GetWindowDC.argtypes = (wintypes.HWND,)
        self.user.GetWindowDC.restype = wintypes.HDC
        self.user.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
        self.user.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
        self.user.PrintWindow.restype = wintypes.BOOL
        self.gdi.CreateCompatibleDC.argtypes = (wintypes.HDC,)
        self.gdi.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
        self.gdi.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        self.gdi.SelectObject.argtypes = (wintypes.HDC, wintypes.HGDIOBJ)
        self.gdi.SelectObject.restype = wintypes.HGDIOBJ
        self.gdi.GetDIBits.argtypes = (wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(_BitmapInfo), wintypes.UINT)
        self.gdi.GetDIBits.restype = ctypes.c_int
        self.gdi.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
        self.gdi.DeleteDC.argtypes = (wintypes.HDC,)
        dc = self.user.GetWindowDC(window.window_handle)
        memory = self.gdi.CreateCompatibleDC(dc)
        bitmap = self.gdi.CreateCompatibleBitmap(dc, width, height)
        old = self.gdi.SelectObject(memory, bitmap)
        try:
            if not dc or not memory or not bitmap or not self.user.PrintWindow(window.window_handle, memory, 2):
                raise NavigationDesktopError("navigation_exact_window_capture_failed")
            self.gdi.SelectObject(memory, old)
            old = None
            info = _BitmapInfo()
            info.bmiHeader = _BitmapInfoHeader(ctypes.sizeof(_BitmapInfoHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            buffer = ctypes.create_string_buffer(width * height * 4)
            if self.gdi.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(info), 0) != height:
                raise NavigationDesktopError("navigation_exact_window_capture_failed")
            return bytes(buffer.raw)
        finally:
            if old:
                self.gdi.SelectObject(memory, old)
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            if memory:
                self.gdi.DeleteDC(memory)
            if dc:
                self.user.ReleaseDC(window.window_handle, dc)

    def click(self, window, x, y):
        if not self.transport._point_belongs_to_window(window, x, y):
            raise NavigationDesktopError("navigation_click_window_mismatch")
        if self.transport._send_guest_click(x, y) != 3:
            raise NavigationDesktopError("navigation_click_incomplete")


class WindowsNavigationCommandHandler:
    """Handler owns no registered row or COM control across command returns."""
    def __init__(self, config, revoked, deadline_at, *, transport=None, surface=None):
        self.config = WindowsNavigationConfig.model_validate(config)
        self.revoked, self.deadline_at = revoked, deadline_at
        self._display_state = self._dpi_state = None
        if surface is None:
            if os.name != "nt" or os.environ.get("PERSONAL_MESSENGER_VM_GUEST") != "1":
                raise NavigationDesktopError("navigation_certified_guest_required")
            if self.config.name_container_selector is None or self.config.search_container_selector is None:
                raise NavigationDesktopError("navigation_semantic_containers_missing")
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            self._dpi_state = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            if not self._dpi_state:
                raise NavigationDesktopError("navigation_dpi_context_unavailable")
        self.transport = transport if transport is not None else WindowsUIAQQAccessibility()
        self.surface = surface if surface is not None else _NativeWindowsSurface(self.transport)
        self._frames: dict[str, tuple[NavigationFrame, bytes]] = {}
        if surface is None:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetThreadExecutionState.argtypes = (wintypes.DWORD,)
            kernel.SetThreadExecutionState.restype = wintypes.DWORD
            self._display_state = int(kernel.SetThreadExecutionState(0x80000003))
            if not self._display_state:
                raise NavigationDesktopError("navigation_display_lease_unavailable")

    def close(self):
        self._frames.clear()
        if self._display_state is not None:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.SetThreadExecutionState(self._display_state)
            self._display_state = None
        if self._dpi_state is not None:
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.SetThreadDpiAwarenessContext.argtypes = (ctypes.c_void_p,)
            user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
            user.SetThreadDpiAwarenessContext(self._dpi_state)
            self._dpi_state = None

    def _guard(self, target):
        if self.revoked.is_set() or datetime.now(UTC) >= self.deadline_at:
            raise NavigationDesktopError("navigation_worker_revoked")
        path = Path(self.config.guard_state_path)
        if path.stat().st_size > 32768:
            raise NavigationDesktopError("navigation_guard_invalid")
        guard = NavigationGuardState.model_validate_json(path.read_text(encoding="utf-8"))
        now = datetime.now(UTC)
        if (guard.target != target or guard.run_id != self.config.expected_run_id
                or guard.worker_epoch != self.config.expected_worker_epoch
                or guard.process_id != self.config.window.process_id
                or guard.window_handle != self.config.window.window_handle
                or guard.process_started_at_100ns != self.config.expected_process_started_at_100ns):
            raise NavigationDesktopError("navigation_guard_scope_changed")
        if guard.paused or guard.has_owned_draft or guard.has_commit_obligation:
            raise NavigationDesktopError("navigation_guard_input_disabled")
        if not 0 <= (now - guard.published_at).total_seconds() <= 5 or guard.lease_expires_at <= now:
            raise NavigationDesktopError("navigation_guard_expired")
        return guard

    @staticmethod
    def _screen_rect(item):
        rect = getattr(item, "BoundingRectangle", None)
        if rect is None:
            raise NavigationDesktopError("navigation_uia_rectangle_missing")
        values = [getattr(rect, name, None) for name in ("left", "top", "right", "bottom")]
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in values):
            raise NavigationDesktopError("navigation_uia_rectangle_invalid")
        left, top, right, bottom = (int(value) for value in values)
        if left >= right or top >= bottom:
            raise NavigationDesktopError("navigation_uia_rectangle_invalid")
        return left, top, right, bottom

    @staticmethod
    def _local_rect(item, crop):
        left, top, right, bottom = WindowsNavigationCommandHandler._screen_rect(item)
        x, y, width, height = crop
        if not x <= left < right <= x + width or not y <= top < bottom <= y + height:
            raise NavigationDesktopError("navigation_uia_region_outside_capture")
        return NavigationRect(left=left - x, top=top - y, right=right - x, bottom=bottom - y)

    def _direct_matches(self, container, selector):
        phase = getattr(self.transport, "_active_phase", None)
        children = phase.indexed_children(container) if phase is not None else None
        if children is None:
            children = container.GetChildren()
        return [item for item in children if self.transport._matches(item, selector, (), ())]

    def _search_controls(self, root):
        selector = self.config.search_container_selector
        if selector is None:
            return self.transport._select(root, self.config.search_selector)
        containers = self.transport._select(root, selector)
        if len(containers) != 1:
            raise NavigationDesktopError("navigation_search_container_not_unique")
        return self._direct_matches(containers[0], self.config.search_selector)

    def _name_regions(self, row, crop, row_rect):
        selector = self.config.name_container_selector
        if selector is None:
            names = self.transport._select(row, self.config.name_selector)
            if len(names) != 1:
                raise NavigationDesktopError("navigation_contact_label_unproven")
            container_rect = row_rect
        else:
            containers = self.transport._select(row, selector)
            if len(containers) != 1:
                raise NavigationDesktopError("navigation_name_container_not_unique")
            container_rect = self._local_rect(containers[0], crop)
            if not row_rect.contains(container_rect):
                raise NavigationDesktopError("navigation_contact_label_unproven")
            # QQ may split a nickname at emoji/font runs. Only direct Text
            # siblings under this semantic name container may form the label;
            # secondary-info/time and summary-main/preview stay nested Groups.
            names = self._direct_matches(containers[0], self.config.name_selector)
        if not 1 <= len(names) <= 32:
            raise NavigationDesktopError("navigation_contact_label_unproven")
        fragments = tuple(sorted((self._local_rect(name, crop) for name in names), key=lambda rect: rect.left))
        if any(not container_rect.contains(rect) or not row_rect.contains(rect) for rect in fragments):
            raise NavigationDesktopError("navigation_contact_label_unproven")
        baseline = fragments[0].top, fragments[0].bottom
        if any((rect.top, rect.bottom) != baseline for rect in fragments):
            raise NavigationDesktopError("navigation_contact_label_unproven")
        if any(not -2 <= right.left - left.right <= 2 for left, right in zip(fragments, fragments[1:])):
            raise NavigationDesktopError("navigation_contact_label_unproven")
        union = NavigationRect(left=fragments[0].left, top=baseline[0],
                               right=max(rect.right for rect in fragments), bottom=baseline[1])
        return union, fragments

    def _read_surface(self, target, *, capture):
        guard = self._guard(target)
        bounds, screen, dpi, start = self.surface.snapshot(self.config.window)
        if start != self.config.expected_process_started_at_100ns:
            raise NavigationDesktopError("navigation_process_lifetime_changed")
        sx, sy, sw, sh = screen
        left, top, right, bottom = bounds
        x, y = max(left, sx), max(top, sy)
        width, height = min(right, sx + sw) - x, min(bottom, sy + sh) - y
        if width <= 0 or height <= 0:
            raise NavigationDesktopError("navigation_window_not_on_screen")
        crop = (x, y, width, height)
        regions, visible = [], []
        # Only values leave this read phase. Names are separately selected under
        # each fresh row; whole row pixels (previews) are never whitelisted.
        with self.transport.read_phase(self.config.window) as phase:
            root = phase.root
            lists = self.transport._select(root, self.config.list_selector)
            search = self._search_controls(root)
            if len(lists) != 1 or len(search) != 1:
                raise NavigationDesktopError("navigation_regions_not_unique")
            list_rect = self._local_rect(lists[0], crop)
            search_rect = self._local_rect(search[0], crop)
            regions.append(NavigationRegion(kind="list", bbox=list_rect))
            regions.append(NavigationRegion(kind="search", bbox=search_rect))
            visible.append(search_rect)
            for row in self.transport._select(lists[0], self.config.row_selector):
                offscreen = getattr(row, "IsOffscreen", None)
                if not isinstance(offscreen, bool):
                    raise NavigationDesktopError("navigation_row_metadata_invalid")
                if offscreen:
                    continue
                rx1, ry1, rx2, ry2 = self._screen_rect(row)
                if (not x <= rx1 < rx2 <= x + width or not y <= ry1 < ry2 <= y + height
                        or not x + list_rect.left <= rx1 < rx2 <= x + list_rect.right
                        or not y + list_rect.top <= ry1 < ry2 <= y + list_rect.bottom):
                    # QQ reports bottom partial rows as IsOffscreen=False.
                    # No pixels or candidate are exposed until a whole row is
                    # inside the certified list and capture, never clipped.
                    continue
                row_rect = NavigationRect(left=rx1 - x, top=ry1 - y, right=rx2 - x, bottom=ry2 - y)
                name_rect, name_fragments = self._name_regions(row, crop, row_rect)
                if not list_rect.contains(row_rect):
                    raise NavigationDesktopError("navigation_contact_label_unproven")
                visible.extend(name_fragments)
                regions.append(NavigationRegion(kind="candidate", bbox=name_rect))
                if len(regions) >= 32:
                    break
        pixels = None
        if capture:
            raw = self.surface.capture(self.config.window, bounds)
            raw_width = right - left
            pixels = b"".join(raw[((y - top + line) * raw_width + x - left) * 4:
                                  ((y - top + line) * raw_width + x - left + width) * 4]
                              for line in range(height))
            pixels = mask_bgra(width, height, pixels, tuple(visible))
            # Empty/black PrintWindow results are not rescued by UIA metadata.
            samples = {pixels[(yy * width + xx) * 4:(yy * width + xx) * 4 + 3]
                       for r in visible for yy in range(r.top, r.bottom, max(1, r.height // 32))
                       for xx in range(r.left, r.right, max(1, r.width // 64))}
            if len(samples) < 2 or all(max(sample) < 8 for sample in samples):
                raise NavigationDesktopError("navigation_capture_blank")
        after_guard = self._guard(target)
        if self.surface.snapshot(self.config.window) != (bounds, screen, dpi, start) or after_guard.model_dump(exclude={"published_at"}) != guard.model_dump(exclude={"published_at"}):
            raise NavigationDesktopError("navigation_capture_scope_changed")
        values = dict(
            run_id=guard.run_id, session_epoch=guard.session_epoch, surface_epoch=guard.surface_epoch,
            worker_epoch=guard.worker_epoch, desktop_lease_id=guard.desktop_lease_id, control_revision=guard.control_revision,
            binding_id=target.binding_id, binding_revision=target.binding_revision, process_id=guard.process_id,
            window_handle=guard.window_handle, screen_origin_x=sx, screen_origin_y=sy, screen_width=sw,
            screen_height=sh, crop_origin_x=x, crop_origin_y=y, crop_width=width, crop_height=height, dpi_scale=dpi,
        )
        if capture:
            _, fresh_values, fresh_regions, _, fresh_visible = self._read_surface(target, capture=False)
            if fresh_values != values or fresh_regions != tuple(regions) or fresh_visible != tuple(visible):
                raise NavigationDesktopError("navigation_capture_regions_changed")
        return guard, values, tuple(regions), pixels, tuple(visible)

    def _capture(self, target):
        self._frames.clear()
        entry_guard = self._guard(target)
        entry_surface = self.surface.snapshot(self.config.window)
        bounds, screen, dpi, start = entry_surface
        if start != self.config.expected_process_started_at_100ns:
            raise NavigationDesktopError("navigation_process_lifetime_changed")
        sx, sy, sw, sh = screen
        left, top, right, bottom = bounds
        x, y = max(left, sx), max(top, sy)
        entry_geometry = dict(screen_origin_x=sx, screen_origin_y=sy, screen_width=sw, screen_height=sh,
            crop_origin_x=x, crop_origin_y=y, crop_width=min(right, sx + sw) - x,
            crop_height=min(bottom, sy + sh) - y, dpi_scale=dpi)
        if entry_geometry["crop_width"] <= 0 or entry_geometry["crop_height"] <= 0:
            raise NavigationDesktopError("navigation_window_not_on_screen")

        def unchanged():
            # Both attempts stay inside the original control/window lifetime.
            # A heartbeat may refresh its timestamp, never the lease or scope.
            guard = self._guard(target)
            if (guard.model_dump(exclude={"published_at"}) != entry_guard.model_dump(exclude={"published_at"})
                    or self.surface.snapshot(self.config.window) != entry_surface):
                raise NavigationDesktopError("navigation_capture_scope_changed")

        for attempt in range(2):
            if attempt:
                unchanged()
            try:
                guard, values, regions, pixels, _ = self._read_surface(target, capture=True)
            except Exception as exc:
                if attempt or getattr(exc, "hresult", None) != -2147220991:
                    raise
                # UIA_E_ELEMENTNOTAVAILABLE only: the failed read phase closes
                # before this whole surface is rebuilt. Never retain partial
                # regions/pixels, retry an input, or relax _fresh_frame proofs.
                continue
            if (guard.model_dump(exclude={"published_at"}) != entry_guard.model_dump(exclude={"published_at"})
                    or any(values[key] != value for key, value in entry_geometry.items())):
                raise NavigationDesktopError("navigation_capture_scope_changed")
            frame = NavigationFrame(**values, frame_id=str(uuid4()), captured_at=datetime.now(UTC),
                                    allowed_regions=regions, privacy_mask_applied=True,
                                    png_bytes=_encode_bgra_png(values["crop_width"], values["crop_height"], pixels))
            unchanged()
            self._frames = {frame.frame_id: (frame, pixels)}
            return frame
        raise AssertionError("unreachable")

    def _scope(self, target):
        # Scope is parent control/ownership plus native window geometry, never
        # UI identity or a region proof. Those still require their independent
        # fresh UIA phases in capture, current digest, and the input command.
        guard = self._guard(target)
        bounds, screen, dpi, start = self.surface.snapshot(self.config.window)
        if start != self.config.expected_process_started_at_100ns:
            raise NavigationDesktopError("navigation_process_lifetime_changed")
        sx, sy, sw, sh = screen
        left, top, right, bottom = bounds
        x, y = max(left, sx), max(top, sy)
        width, height = min(right, sx + sw) - x, min(bottom, sy + sh) - y
        if width <= 0 or height <= 0:
            raise NavigationDesktopError("navigation_window_not_on_screen")
        values = dict(
            run_id=guard.run_id, session_epoch=guard.session_epoch, surface_epoch=guard.surface_epoch,
            worker_epoch=guard.worker_epoch, desktop_lease_id=guard.desktop_lease_id, control_revision=guard.control_revision,
            binding_id=target.binding_id, binding_revision=target.binding_revision, process_id=guard.process_id,
            window_handle=guard.window_handle, screen_origin_x=sx, screen_origin_y=sy, screen_width=sw,
            screen_height=sh, crop_origin_x=x, crop_origin_y=y, crop_width=width, crop_height=height, dpi_scale=dpi,
        )
        after_guard = self._guard(target)
        if (self.surface.snapshot(self.config.window) != (bounds, screen, dpi, start)
                or after_guard.model_dump(exclude={"published_at"}) != guard.model_dump(exclude={"published_at"})):
            raise NavigationDesktopError("navigation_capture_scope_changed")
        return NavigationDesktopScope(**values, account_id=target.account_id, conversation_id=target.conversation_id,
            lease_expires_at=guard.lease_expires_at, observed_at=datetime.now(UTC), foreground=True,
            paused=guard.paused, has_owned_draft=guard.has_owned_draft, has_commit_obligation=guard.has_commit_obligation)

    def _local_witness(self, target):
        guard = self._guard(target)
        if guard.observation_epoch is None:
            raise NavigationDesktopError("navigation_observation_epoch_missing")
        bounds, screen, dpi, start = self.surface.snapshot(self.config.window)
        if start != self.config.expected_process_started_at_100ns:
            raise NavigationDesktopError("navigation_process_lifetime_changed")
        with self.transport.read_phase(self.config.window) as phase:
            root = phase.root
            rows = self.transport._select(root, self.config.row_selector)
            selected = []
            for row in rows:
                offscreen = getattr(row, "IsOffscreen", None)
                if not isinstance(offscreen, bool):
                    raise NavigationDesktopError("navigation_selection_metadata_invalid")
                if offscreen:
                    continue
                pattern = get_uia_pattern(row, "GetSelectionItemPattern", 10010)
                token = self.config.row_selector.selected_class_name_token
                is_selected = pattern.IsSelected if pattern is not None else None
                if is_selected is not None and not isinstance(is_selected, bool):
                    raise NavigationDesktopError("navigation_selection_metadata_invalid")
                if is_selected is True:
                    selected.append((row, "selection_pattern"))
                elif token is not None and token in str(getattr(row, "ClassName", "")).split():
                    selected.append((row, "selected_class"))
            row_token = selected_runtime_token(selected[0][0].GetRuntimeId()) if len(selected) == 1 else None
            source = selected[0][1] if len(selected) == 1 else None
            headers = self.transport._select(root, self.config.header_selector)
            header = hashlib.sha256(str(getattr(headers[0], "Name", "")).encode("utf-8")).hexdigest() if len(headers) == 1 else None
            groups = []
            for item in self.transport._descendants(root):
                class_name = getattr(item, "ClassName", "")
                if not isinstance(class_name, str):
                    raise NavigationDesktopError("navigation_group_metadata_invalid")
                if _GROUP_MARKERS & set(class_name.split()):
                    groups.append(item)
            messages = self.transport._select(root, self.config.message_selector)
            composers = self.transport._select(root, self.config.composer_selector)
            tail = empty = None
            if len(messages) == 1:
                try:
                    # Reuse the exact semantic-region and metadata proof. A
                    # near-tail percentage, coercion, or missing ScrollPattern
                    # never becomes a latest-tail witness.
                    tail = self.transport.message_tail_is_latest(self.config.window, self.config.message_selector)
                except UIAUnavailable:
                    tail = None
                if tail is not None and not isinstance(tail, bool):
                    raise NavigationDesktopError("navigation_tail_metadata_invalid")
            if len(composers) == 1:
                empty = read_composer_text(composers[0]) == ""
            structure = None
            if len(headers) == len(messages) == len(composers) == 1:
                structure = current_chat_structure_digest(**{
                    role: (str(getattr(item, "ClassName", "")), item.GetRuntimeId())
                    for role, item in zip(("header", "messages", "composer"),
                                          (headers[0], messages[0], composers[0]))
                })
            surface_kind = "unknown"
            if not groups:
                if not headers and not messages and not composers and len(selected) <= 1:
                    surface_kind = "non_chat"
                elif len(headers) == len(messages) == len(composers) == len(selected) == 1:
                    surface_kind = "chat"
        after = self._guard(target)
        if after.model_dump(exclude={"published_at"}) != guard.model_dump(exclude={"published_at"}) or self.surface.snapshot(self.config.window) != (bounds, screen, dpi, start):
            raise NavigationDesktopError("navigation_witness_scope_changed")
        # Message bodies, search contents, drafts and names do not cross IPC.
        return CurrentChatWitness(
            account_id=target.account_id, conversation_id=target.conversation_id,
            binding_id=target.binding_id, binding_revision=target.binding_revision,
            run_id=guard.run_id, session_epoch=guard.session_epoch, surface_epoch=guard.surface_epoch,
            worker_epoch=guard.worker_epoch, observation_epoch=guard.observation_epoch,
            desktop_lease_id=guard.desktop_lease_id, control_revision=guard.control_revision,
            process_id=guard.process_id, process_started_at_100ns=start, window_handle=guard.window_handle,
            captured_at=datetime.now(UTC), captured_monotonic_ns=time.monotonic_ns(),
            surface_kind=surface_kind, header_candidate_count=len(headers),
            message_candidate_count=len(messages), composer_candidate_count=len(composers),
            header_digest=header, selected_row_runtime_id_hash=row_token, selected_row_candidate_count=len(selected),
            selected_row_selection_source=source, active_chat_structure_digest=structure,
            conversation_type="group" if groups else "direct" if structure is not None else "unknown",
            group_marker_probe_complete=True, group_marker_count=len(groups), latest_tail=tail, composer_empty=empty,
        )

    def _owned_frame_pixels(self, command):
        if not 0 <= (datetime.now(UTC) - command.frame.captured_at).total_seconds() <= 20:
            raise NavigationDesktopError("navigation_frame_expired")
        saved = self._frames.get(command.frame.frame_id)
        if saved is None or saved[0] != command.frame:
            raise NavigationDesktopError("navigation_frame_not_owned")
        return saved[1]

    def _fresh_frame(self, command):
        expected = self._owned_frame_pixels(command)
        guard, values, regions, pixels, _ = self._read_surface(command.target, capture=True)
        for key, value in values.items():
            if getattr(command.frame, key) != value:
                raise NavigationDesktopError("navigation_frame_scope_changed")
        if regions != command.frame.allowed_regions:
            raise NavigationDesktopError("navigation_regions_changed")
        return expected, pixels

    def __call__(self, command):
        command = NavigationWorkerCommand.model_validate(command)
        self.deadline_at = min(self.deadline_at, command.deadline_at)
        target = command.target
        self._guard(target)
        if command.action == "capture":
            return NavigationWorkerResult(request_id=command.request_id, frame=self._capture(target))
        if command.action == "scope":
            return NavigationWorkerResult(request_id=command.request_id, scope=self._scope(target))
        if command.action == "local_witness":
            return NavigationWorkerResult(request_id=command.request_id, witness=self._local_witness(target))
        if command.action == "digest":
            if command.region not in command.frame.allowed_regions:
                raise NavigationDesktopError("navigation_region_not_owned")
            # Frozen proof uses only the original owned masked pixels. The
            # separate current=True call independently renders the live ROI.
            pixels = self._fresh_frame(command)[1] if command.current else self._owned_frame_pixels(command)
            return NavigationWorkerResult(request_id=command.request_id, digest=_digest_pixels(pixels, command.frame.crop_width, command.region.bbox))
        if command.action == "verified_click":
            # The explicit producer capability accepts only the exact owned
            # candidate/search region containing this physical input point.
            # No caller-supplied digest or previously checked live frame is used.
            region = command.region
            if region not in command.frame.allowed_regions or region.kind not in {"candidate", "search"}:
                raise NavigationDesktopError("navigation_region_not_owned")
            lx, ly = command.x - command.frame.crop_origin_x, command.y - command.frame.crop_origin_y
            candidates = [r for r in command.frame.allowed_regions if r.kind in {"candidate", "search"}
                          and r.bbox.left <= lx < r.bbox.right and r.bbox.top <= ly < r.bbox.bottom]
            if candidates != [region]:
                raise NavigationDesktopError("navigation_input_region_unproven")
        expected, actual = self._fresh_frame(command)
        region = command.region
        if region is None:
            lx, ly = command.x - command.frame.crop_origin_x, command.y - command.frame.crop_origin_y
            kind = "list" if command.action == "scroll" else None
            candidates = [r for r in command.frame.allowed_regions if (kind is None and r.kind in {"candidate", "search"} or r.kind == kind)
                          and r.bbox.left <= lx < r.bbox.right and r.bbox.top <= ly < r.bbox.bottom]
            if len(candidates) != 1:
                raise NavigationDesktopError("navigation_input_region_unproven")
            region = candidates[0]
        if region not in command.frame.allowed_regions or _digest_pixels(expected, command.frame.crop_width, region.bbox) != _digest_pixels(actual, command.frame.crop_width, region.bbox):
            raise NavigationDesktopError("navigation_region_changed")
        if command.action in {"search_focus", "set_query", "scroll"}:
            selector = self.config.search_selector if command.action != "scroll" else self.config.list_selector
            with self.transport.read_phase(self.config.window) as phase:
                controls = (self._search_controls(phase.root) if command.action != "scroll"
                            else self.transport._select(phase.root, selector))
                if len(controls) != 1 or self._local_rect(controls[0], (command.frame.crop_origin_x, command.frame.crop_origin_y, command.frame.crop_width, command.frame.crop_height)) != region.bbox:
                    raise NavigationDesktopError("navigation_action_control_changed")
                control = controls[0]
                if command.action == "scroll":
                    pattern = get_uia_pattern(control, "GetScrollPattern", 10004)
                    if pattern is None or pattern.VerticallyScrollable is not True:
                        raise NavigationDesktopError("navigation_list_scroll_unavailable")
                    self._last_boundary(target, command.frame)
                    for _ in range(command.amount):
                        self._last_boundary(target, command.frame)
                        pattern.Scroll(2, 1 if command.direction == "up" else 4)  # NoAmount, SmallDecrement/Increment
                else:
                    focused = self.transport._composer_focused(control, self.config.window)
                    if command.action == "search_focus":
                        return NavigationWorkerResult(request_id=command.request_id, focused=focused)
                    if not focused:
                        raise NavigationDesktopError("navigation_search_focus_unproven")
                    pattern = get_uia_pattern(control, "GetValuePattern", 10002)
                    if pattern is None or pattern.IsReadOnly is not False:
                        raise NavigationDesktopError("navigation_search_value_unavailable")
                    query = target.trusted_queries[command.query_alias_index]
                    if not self.transport._composer_focused(control, self.config.window):
                        raise NavigationDesktopError("navigation_search_focus_unproven")
                    self._last_boundary(target, command.frame)
                    pattern.SetValue(query)  # no Enter, SendKeys, or composer fallback
                    if str(pattern.Value) != query:
                        raise NavigationDesktopError("navigation_search_write_unconfirmed")
        elif command.action in {"click", "verified_click"}:
            self._last_boundary(target, command.frame)
            self.surface.click(self.config.window, command.x, command.y)
        else:
            raise NavigationDesktopError("navigation_overlay_not_supported")
        self._frames.clear()  # One action consumes the screenshot even on unchanged pixels.
        return NavigationWorkerResult(request_id=command.request_id, completed=True)

    def _last_boundary(self, target, frame):
        guard = self._guard(target)
        bounds, screen, dpi, start = self.surface.snapshot(self.config.window)
        sx, sy, sw, sh = screen
        left, top, right, bottom = bounds
        x, y = max(left, sx), max(top, sy)
        values = {
            "run_id": guard.run_id, "session_epoch": guard.session_epoch, "surface_epoch": guard.surface_epoch,
            "worker_epoch": guard.worker_epoch, "desktop_lease_id": guard.desktop_lease_id, "control_revision": guard.control_revision,
            "screen_origin_x": sx, "screen_origin_y": sy, "screen_width": sw, "screen_height": sh,
            "crop_origin_x": x, "crop_origin_y": y, "crop_width": min(right, sx + sw) - x,
            "crop_height": min(bottom, sy + sh) - y, "dpi_scale": dpi,
        }
        if start != self.config.expected_process_started_at_100ns or any(getattr(frame, name) != value for name, value in values.items()):
            raise NavigationDesktopError("navigation_last_boundary_changed")
        # No await occurs between this check and the synchronous native input.
        if self.revoked.is_set() or datetime.now(UTC) >= self.deadline_at:
            raise NavigationDesktopError("navigation_worker_revoked")


def _windows_handler_factory(configuration, revoked, deadline_at):
    return WindowsNavigationCommandHandler(configuration, revoked, deadline_at)


class WindowsNavigationBackend:
    """Parent adapter. All raw pixels and COM stay inside the guest worker."""
    def __init__(self, config: WindowsNavigationConfig, target: ContactTarget, *, deadline_at: datetime, worker=None):
        self.config, self.target, self.deadline_at = config, target, deadline_at
        self.worker = worker or NavigationWorkerProcess(_windows_handler_factory, config.model_dump(), deadline_at=deadline_at)

    def revoke(self):
        self.worker.revoke()

    def close(self):
        self.worker.close()

    async def _request(self, action, *, target=None, cancel_event=None, deadline_at=None, **arguments):
        actual = target or self.target
        if actual != self.target:
            raise NavigationDesktopError("navigation_backend_target_changed")
        result = await self.worker.request(NavigationWorkerCommand(action=action, target=actual,
            deadline_at=min(deadline_at or self.deadline_at, self.deadline_at), **arguments), cancel_event=cancel_event)
        field = {"capture": "frame", "scope": "scope", "local_witness": "witness", "digest": "digest", "search_focus": "focused"}.get(action, "completed")
        if getattr(result, field) is None or field == "completed" and not result.completed:
            raise NavigationDesktopError("navigation_worker_outcome_mismatch")
        return result

    async def bound_capture(self, target, *, deadline_at):
        return (await self._request("capture", target=target, deadline_at=deadline_at)).frame

    async def current_scope(self, target):
        return (await self._request("scope", target=target)).scope

    async def local_witness(self, target):
        return (await self._request("local_witness", target=target)).witness

    async def relevant_region_digest(self, frame, region, *, current):
        return (await self._request("digest", frame=frame, region=region, current=current)).digest

    async def search_focused(self, frame, region):
        return (await self._request("search_focus", frame=frame, region=region)).focused

    async def click(self, frame, x, y, *, deadline_at, cancel_event):
        await self._request("click", frame=frame, x=x, y=y, cancel_event=cancel_event, deadline_at=deadline_at)

    async def verified_click(self, frame, region, x, y, *, deadline_at, cancel_event):
        await self._request("verified_click", frame=frame, region=region, x=x, y=y,
                            cancel_event=cancel_event, deadline_at=deadline_at)

    async def scroll(self, frame, x, y, direction, amount, *, deadline_at, cancel_event):
        await self._request("scroll", frame=frame, x=x, y=y, direction=direction, amount=amount, cancel_event=cancel_event, deadline_at=deadline_at)

    async def set_query(self, frame, region, query, *, deadline_at, cancel_event):
        if query not in self.target.trusted_queries:
            raise NavigationDesktopError("navigation_query_not_registered")
        await self._request("set_query", frame=frame, region=region, query_alias_index=self.target.trusted_queries.index(query), cancel_event=cancel_event, deadline_at=deadline_at)

    async def dismiss_known(self, frame, region, x, y, *, deadline_at, cancel_event):
        raise NavigationDesktopError("navigation_overlay_not_supported")


class ProcessScopedDesktopOperator(ScopedDesktopOperator):
    """Keep desktop ownership until the owned worker is confirmed gone.

    Each instance owns one finite worker lifetime. A failed reap deliberately
    leaves the shared desktop lock held; a trusted owner may retry cleanup but
    cannot start another navigation episode on the still-owned desktop.
    """

    cleanup_reserve_seconds = 0.5

    def __init__(self, **arguments):
        super().__init__(**arguments)
        self._failed_cleanup = None
        self._cleanup_lock = asyncio.Lock()

    def revoke_round(self):
        try:
            self.backend.revoke()  # synchronous IPC before asynchronous cleanup
        finally:
            super().revoke_round()

    @asynccontextmanager
    async def round(self, target, *, deadline_at, cancel_event=None):
        if self._round_capability.get() is not None:
            # Coordinator child tasks inherit the capability. Only the outer
            # owner closes the worker, never a nested capture or input step.
            async with super().round(target, deadline_at=deadline_at, cancel_event=cancel_event):
                yield
            return
        if self._failed_cleanup is not None:
            raise NavigationDesktopError("navigation_worker_cleanup_required")
        context = super().round(target, deadline_at=deadline_at, cancel_event=cancel_event)
        await context.__aenter__()
        try:
            yield
        finally:
            try:
                self.revoke_round()
                # This bounded synchronous terminate/reap precedes releasing
                # the desktop lock. No model or COM await occurs here.
                self.backend.close()
            except BaseException:
                # Retain the generator so its finally cannot release the lock
                # while an owned input worker may still be alive.
                self._failed_cleanup = context
                raise
            await context.__aexit__(None, None, None)

    async def retry_cleanup(self) -> None:
        """Release only this operator's retained lock after a successful reap."""
        if self._round_capability.get() is not None:
            raise NavigationDesktopError("navigation_cleanup_context_not_clear")
        async with self._cleanup_lock:
            context = self._failed_cleanup
            if context is None:
                return
            self.backend.revoke()
            self.backend.close()  # Failure preserves both context and lock.
            try:
                await context.__aexit__(None, None, None)
            finally:
                # Base context releases the lock even if waiter cleanup is
                # cancelled. Its token reset affects only this empty context.
                self._failed_cleanup = None
