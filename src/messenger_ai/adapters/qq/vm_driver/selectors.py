from __future__ import annotations

from messenger_ai.adapters.qq.models import QQSelectorPack

REQUIRED_SELECTOR_NAMES = frozenset({"main_window", "conversations", "conversation_item", "composer", "send", "bubbles"})


def validate_guest_selector_pack(pack: QQSelectorPack) -> None:
    """Reject incomplete packs; selectors are versioned guest deployment data."""
    names = {selector.name for selector in pack.selectors}
    missing = REQUIRED_SELECTOR_NAMES - names
    if missing:
        raise ValueError(f"selector pack misses required entries: {sorted(missing)}")
    if len(names) != len(pack.selectors):
        raise ValueError("selector pack has duplicate names")
