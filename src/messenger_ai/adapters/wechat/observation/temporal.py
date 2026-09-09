"""Seek-safe frame differencing and duplicate suppression."""

from __future__ import annotations

from collections import OrderedDict

from .models import CapturedFrame


class TemporalFrameDiffer:
    def __init__(self, max_history: int = 16) -> None:
        self._seen: OrderedDict[str, str] = OrderedDict()
        self.max_history = max(1, max_history)

    def is_duplicate(self, frame: CapturedFrame) -> bool:
        previous = self._seen.get(frame.binding_id)
        self._seen[frame.binding_id] = frame.frame_hash
        self._seen.move_to_end(frame.binding_id)
        while len(self._seen) > self.max_history:
            self._seen.popitem(last=False)
        return previous == frame.frame_hash

    def previous_hash(self, binding_id: str) -> str | None:
        return self._seen.get(binding_id)

    def consistency(self, frame: CapturedFrame) -> float:
        previous = self._seen.get(frame.binding_id)
        if previous is None:
            return 0.5
        return 1.0 if previous == frame.frame_hash else 0.75

    def reset(self, binding_id: str | None = None) -> None:
        if binding_id is None:
            self._seen.clear()
        else:
            self._seen.pop(binding_id, None)
