"""Local OCR seam and an optional Docker sidecar contract.

The sidecar class only describes an injected endpoint.  It does not pull an
image, start a container, upload data, or become a core dependency.
"""

from __future__ import annotations

from collections.abc import Callable

from .models import CapturedFrame, OCRResult, RegionOfInterest


class UnavailableLocalOCR:
    def recognize(self, frame: CapturedFrame, roi: RegionOfInterest) -> OCRResult:
        return OCRResult(
            engine="unavailable",
            confidence=0,
            source_frame_hash=frame.frame_hash,
            diagnostics={"reason": "no_local_ocr_provider"},
        )


class DockerOCRSidecar:
    """Optional adapter around a caller-provided local request function.

    ``request`` is deliberately injected so core code never depends on Docker
    or an unverified image.  The request function must stay local and return an
    ``OCRResult``; endpoint policy belongs to deployment configuration.
    """

    def __init__(
        self, request: Callable[[CapturedFrame, RegionOfInterest], OCRResult]
    ) -> None:
        self._request = request

    def recognize(self, frame: CapturedFrame, roi: RegionOfInterest) -> OCRResult:
        return self._request(frame, roi)


LocalOCR = UnavailableLocalOCR
