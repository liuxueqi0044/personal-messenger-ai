import pytest

from messenger_ai.adapters.qq.vm_driver.sequence_alignment import (
    SnapshotAlignmentError,
    unique_suffix_start,
)


def test_unchanged_snapshot_has_no_suffix() -> None:
    assert unique_suffix_start(["a", "b"], ["a", "b"]) == 2


def test_repeated_text_with_unique_context_aligns() -> None:
    assert unique_suffix_start(["a", "same", "b"], ["same", "b", "same"]) == 2


def test_periodic_snapshot_with_multiple_overlaps_is_ambiguous() -> None:
    with pytest.raises(SnapshotAlignmentError, match="message_anchor_ambiguous"):
        unique_suffix_start(["a", "b", "a", "b"], ["a", "b", "a", "b", "c"])


def test_missing_anchor_fails_closed() -> None:
    with pytest.raises(SnapshotAlignmentError, match="message_anchor_gap"):
        unique_suffix_start(["a", "b"], ["c", "d"])
