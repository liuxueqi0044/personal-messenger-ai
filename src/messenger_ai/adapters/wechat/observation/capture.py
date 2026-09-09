"""Capture/ROI implementations that enforce the target-window boundary."""

from __future__ import annotations

from datetime import UTC, datetime

from .models import CapturedFrame, Rectangle, RegionOfInterest, WindowBinding
from .ports import WindowCapturePort


class CaptureBoundaryError(RuntimeError):
    """Raised when a provider attempts to capture outside the bound window."""


class SyntheticCaptureProvider:
    """Deterministic capture port for redacted tests and replay fixtures."""

    def __init__(self, payload: bytes = b"", metadata: dict | None = None) -> None:
        self.payload = payload
        self.metadata = metadata or {}
        self.calls: list[tuple[str, Rectangle]] = []

    def capture(self, binding: WindowBinding, roi: Rectangle) -> CapturedFrame:
        bounds = Rectangle(
            x=0,
            y=0,
            width=binding.environment.window_width,
            height=binding.environment.window_height,
        )
        if not bounds.contains(roi):
            raise CaptureBoundaryError("ROI is outside the bound WeChat window")
        self.calls.append((binding.binding_id, roi))
        return CapturedFrame(
            frame_id=f"synthetic-{len(self.calls)}",
            captured_at=datetime.now(UTC),
            width=roi.width,
            height=roi.height,
            binding_id=binding.binding_id,
            roi=roi,
            pixels=self.payload,
            metadata=dict(self.metadata),
        )


WindowCaptureProvider = WindowCapturePort


class WindowRegionDetector:
    """Use classifier-supplied regions; never infer a desktop-sized region."""

    def detect(self, frame: CapturedFrame, layout) -> tuple[RegionOfInterest, ...]:
        regions = tuple(
            region
            for region in (layout.conversation_roi, layout.message_roi)
            if region is not None
        )
        if not regions:
            return ()
        return tuple(region for region in regions if frame.roi.contains(region.rect))
