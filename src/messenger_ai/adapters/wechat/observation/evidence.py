"""TTL evidence store and redacted audit logging."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256

from .models import (
    ObservationEvidence,
    RegionOfInterest,
    VisualObservation,
    evidence_expiry,
)


class EvidenceStore:
    def __init__(self) -> None:
        self._items: dict[str, ObservationEvidence] = {}

    def put(self, evidence: ObservationEvidence) -> None:
        self._items[evidence.evidence_id] = evidence

    def get(self, evidence_id: str, now: datetime) -> ObservationEvidence | None:
        evidence = self._items.get(evidence_id)
        if evidence is None:
            return None
        if evidence.is_expired(now):
            self._items.pop(evidence_id, None)
            return None
        return evidence

    def purge(self, now: datetime) -> int:
        expired = [key for key, value in self._items.items() if value.is_expired(now)]
        for key in expired:
            self._items.pop(key, None)
        return len(expired)


class HumanEvidenceQueue:
    def __init__(self, store: EvidenceStore | None = None) -> None:
        self.store = store or EvidenceStore()
        self._queue: list[VisualObservation] = []

    def enqueue(self, observation: VisualObservation) -> None:
        if observation.human_review_required:
            self.store.put(observation.evidence)
            self._queue.append(observation)

    def record(self, observation: VisualObservation) -> None:
        """Retain accepted evidence for its TTL without putting it in review."""
        self.store.put(observation.evidence)

    def pending(self, now: datetime) -> tuple[VisualObservation, ...]:
        self.store.purge(now)
        self._queue = [
            item for item in self._queue if not item.evidence.is_expired(now)
        ]
        return tuple(self._queue)


class RedactedObservationLogger:
    """Collect only hashes, dimensions and confidence; never OCR text/pixels."""

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []

    def record(self, observation: VisualObservation) -> dict[str, object]:
        record = {
            "observation_id": observation.observation_id,
            "binding_id": observation.binding_id,
            "frame_hash": observation.frame_hash,
            "regions": [region.name for region in observation.evidence.regions],
            "status": observation.status.value,
            "confidence": observation.confidence.overall,
            "message_count": len(observation.messages),
            "summary_hash": sha256(
                observation.evidence.redacted_summary.encode()
            ).hexdigest(),
        }
        self.records.append(record)
        return record


def make_evidence(
    *,
    evidence_id: str,
    frame_hash: str,
    binding_id: str,
    regions: tuple[RegionOfInterest, ...],
    created_at: datetime,
    ttl_seconds: float,
    summary: str = "",
) -> ObservationEvidence:
    # ``summary`` is supplied by the caller as an already-redacted label.
    return ObservationEvidence(
        evidence_id=evidence_id,
        frame_hash=frame_hash,
        binding_id=binding_id,
        regions=regions,
        redacted_summary=summary[:160],
        created_at=created_at,
        expires_at=evidence_expiry(created_at, ttl_seconds),
    )
