from __future__ import annotations

from collections.abc import Sequence
from typing import TypeVar


T = TypeVar("T")


class SnapshotAlignmentError(ValueError):
    """The old snapshot cannot be aligned to the new one uniquely."""


def unique_suffix_start(before: Sequence[T], after: Sequence[T]) -> int:
    """Return the new-tail offset when suffix/prefix alignment is unique."""
    if list(before) == list(after):
        return len(before)
    if not before:
        return 0
    matches = [
        overlap
        for overlap in range(1, min(len(before), len(after)) + 1)
        if list(before[-overlap:]) == list(after[:overlap])
    ]
    if not matches:
        raise SnapshotAlignmentError("message_anchor_gap")
    if len(matches) != 1:
        raise SnapshotAlignmentError("message_anchor_ambiguous")
    return matches[0]
