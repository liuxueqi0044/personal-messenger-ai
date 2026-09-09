"""Conservative layout classification for injected/replay frames."""

from __future__ import annotations

from .models import (
    CapturedFrame,
    LayoutClassification,
    Rectangle,
    RegionOfInterest,
    WindowEnvironment,
)


class MetadataLayoutClassifier:
    """Classifier seam used by tests until a real local vision model is chosen.

    It accepts only explicit metadata supplied by a trusted fixture/provider;
    unknown or partially described layouts are unsupported.
    """

    def classify(
        self, frame: CapturedFrame, environment: WindowEnvironment
    ) -> LayoutClassification:
        metadata = frame.metadata
        version = metadata.get("layout_version")
        confidence = float(metadata.get("layout_confidence", 0))
        boundary = float(metadata.get("message_boundary_confidence", confidence))
        message_rect = metadata.get("message_rect")
        conversation_rect = metadata.get("conversation_rect")
        if not version or not message_rect or not conversation_rect:
            return LayoutClassification(
                confidence=0,
                message_boundary_confidence=0,
                supported=False,
                reason="layout_metadata_missing",
            )
        try:
            message_roi = RegionOfInterest(
                name="messages",
                purpose="message_bubbles",
                rect=Rectangle(**message_rect),
            )
            conversation_roi = RegionOfInterest(
                name="conversation",
                purpose="conversation_identity",
                rect=Rectangle(**conversation_rect),
            )
        except (TypeError, ValueError):
            return LayoutClassification(
                confidence=0,
                message_boundary_confidence=0,
                supported=False,
                reason="layout_metadata_invalid",
            )
        frame_bounds = frame.roi
        supported = (
            frame_bounds.contains(message_roi.rect)
            and frame_bounds.contains(conversation_roi.rect)
            and 0 <= confidence <= 1
            and 0 <= boundary <= 1
        )
        return LayoutClassification(
            layout_version=str(version),
            confidence=confidence,
            message_boundary_confidence=boundary,
            conversation_roi=conversation_roi,
            message_roi=message_roi,
            supported=supported,
            reason="" if supported else "layout_roi_outside_bound",
        )


LayoutClassifier = MetadataLayoutClassifier
