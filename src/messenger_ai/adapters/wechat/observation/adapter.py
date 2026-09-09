"""Composable WeChat visual observation adapter."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from .assembly import MessageAssembler
from .capture import WindowRegionDetector
from .evidence import HumanEvidenceQueue, RedactedObservationLogger, make_evidence
from .models import (
    ConfidenceBreakdown,
    LayoutClassification,
    MessageDirection,
    ObservationStatus,
    Rectangle,
    VisualObservation,
    WindowBinding,
)
from .normalize import WeChatEventNormalizer
from .ocr import UnavailableLocalOCR
from .ports import (
    LayoutClassifierPort,
    LocalOCRPort,
    RegionOfInterestPort,
    WindowCapturePort,
)
from .temporal import TemporalFrameDiffer


class WeChatObservationAdapter:
    """Run one read-only frame through the M4 pipeline.

    No provider is allowed to discover, activate, or control a window.  Every
    capture is scoped to ``binding`` and its window-sized ROI; all external
    dependencies are constructor-injected.
    """

    def __init__(
        self,
        capture: WindowCapturePort,
        *,
        layout_classifier: LayoutClassifierPort,
        ocr: LocalOCRPort | None = None,
        roi_detector: RegionOfInterestPort | None = None,
        differ: TemporalFrameDiffer | None = None,
        assembler: MessageAssembler | None = None,
        normalizer: WeChatEventNormalizer | None = None,
        evidence_queue: HumanEvidenceQueue | None = None,
        logger: RedactedObservationLogger | None = None,
        evidence_ttl_seconds: float = 30,
    ) -> None:
        self.capture = capture
        self.layout_classifier = layout_classifier
        self.ocr = ocr or UnavailableLocalOCR()
        self.roi_detector = roi_detector or WindowRegionDetector()
        self.differ = differ or TemporalFrameDiffer()
        self.assembler = assembler or MessageAssembler()
        self.normalizer = normalizer or WeChatEventNormalizer()
        self.evidence_queue = evidence_queue or HumanEvidenceQueue()
        self.logger = logger or RedactedObservationLogger()
        self.evidence_ttl_seconds = evidence_ttl_seconds

    def observe(
        self, binding: WindowBinding, *, now: datetime | None = None
    ) -> VisualObservation | None:
        observed_at = now or datetime.now(UTC)
        root = Rectangle(
            x=0,
            y=0,
            width=binding.environment.window_width,
            height=binding.environment.window_height,
        )
        frame = self.capture.capture(binding, root)
        previous = self.differ.previous_hash(binding.binding_id)
        if self.differ.is_duplicate(frame):
            return None
        temporal_confidence = 1.0 if previous is None else 0.75
        layout = self.layout_classifier.classify(frame, binding.environment)
        regions = self.roi_detector.detect(frame, layout)
        ocr_result = None
        messages = ()
        if layout.supported and layout.message_roi is not None:
            ocr_result = self.ocr.recognize(frame, layout.message_roi)
            messages = self.assembler.assemble(ocr_result, layout)
        else:
            ocr_result = None
        direction_confidence = self._direction_confidence(messages)
        ocr_confidence = ocr_result.confidence if ocr_result else 0.0
        confidence = ConfidenceBreakdown(
            conversation_identity_confidence=binding.identity_confidence,
            layout_version_confidence=layout.confidence if layout.supported else 0,
            message_boundary_confidence=layout.message_boundary_confidence,
            ocr_text_confidence=ocr_confidence,
            direction_confidence=direction_confidence,
            temporal_consistency_confidence=temporal_confidence,
        )
        reason = self._reason(layout, messages, ocr_result, confidence)
        status = (
            ObservationStatus.ACCEPTED
            if confidence.hard_gates_pass
            else ObservationStatus.HUMAN_REVIEW
        )
        evidence = make_evidence(
            evidence_id=str(uuid4()),
            frame_hash=frame.frame_hash,
            binding_id=binding.binding_id,
            regions=regions,
            created_at=observed_at,
            ttl_seconds=self.evidence_ttl_seconds,
            summary=reason,
        )
        observation = VisualObservation(
            observation_id=str(uuid4()),
            binding_id=binding.binding_id,
            conversation_id=binding.identity,
            frame_hash=frame.frame_hash,
            observed_at=observed_at,
            messages=messages,
            confidence=confidence,
            evidence=evidence,
            status=status,
            human_review_required=status != ObservationStatus.ACCEPTED,
            reason=reason,
        )
        if observation.human_review_required:
            self.evidence_queue.enqueue(observation)
        else:
            self.evidence_queue.record(observation)
        self.logger.record(observation)
        return observation

    def poll_events(self, binding: WindowBinding, *, now: datetime | None = None):
        observation = self.observe(binding, now=now)
        if observation is None:
            return ()
        return self.normalizer.normalize_messages(observation)

    @staticmethod
    def _direction_confidence(messages) -> float:
        if not messages:
            return 0.0
        if any(item.direction == MessageDirection.UNKNOWN for item in messages):
            return 0.0
        return min(item.confidence for item in messages)

    @staticmethod
    def _reason(
        layout: LayoutClassification,
        messages,
        ocr_result,
        confidence: ConfidenceBreakdown,
    ) -> str:
        if not layout.supported:
            return layout.reason or "unsupported_layout"
        if not messages:
            return "no_text_messages" if ocr_result else "ocr_not_run"
        if not confidence.hard_gates_pass:
            failed = []
            if (
                confidence.conversation_identity_confidence
                < confidence.hard_identity_threshold
            ):
                failed.append("identity")
            if confidence.layout_version_confidence < confidence.hard_layout_threshold:
                failed.append("layout")
            if confidence.direction_confidence < confidence.hard_direction_threshold:
                failed.append("direction")
            return "hard_gate_failed:" + ",".join(failed)
        return ""
