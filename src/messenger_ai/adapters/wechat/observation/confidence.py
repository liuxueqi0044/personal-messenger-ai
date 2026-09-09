"""Independent hard-gate confidence calibration."""

from __future__ import annotations

from .models import ConfidenceBreakdown


class ConfidenceCalibrator:
    def __init__(
        self,
        *,
        identity_threshold: float = 0.95,
        layout_threshold: float = 0.90,
        direction_threshold: float = 0.90,
    ) -> None:
        self.identity_threshold = identity_threshold
        self.layout_threshold = layout_threshold
        self.direction_threshold = direction_threshold

    def calibrate(
        self,
        *,
        conversation_identity_confidence: float,
        layout_version_confidence: float,
        message_boundary_confidence: float,
        ocr_text_confidence: float,
        direction_confidence: float,
        temporal_consistency_confidence: float,
    ) -> ConfidenceBreakdown:
        return ConfidenceBreakdown(
            conversation_identity_confidence=conversation_identity_confidence,
            layout_version_confidence=layout_version_confidence,
            message_boundary_confidence=message_boundary_confidence,
            ocr_text_confidence=ocr_text_confidence,
            direction_confidence=direction_confidence,
            temporal_consistency_confidence=temporal_consistency_confidence,
            hard_identity_threshold=self.identity_threshold,
            hard_layout_threshold=self.layout_threshold,
            hard_direction_threshold=self.direction_threshold,
        )
