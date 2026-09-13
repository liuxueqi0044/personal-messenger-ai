import pytest

from messenger_ai.adapters.qq.vm_driver.conversation_type import (
    ConversationStructureError,
    assess_conversation_structure,
)


def report(*classes, truncated=False):
    nodes = [{"class_name": value} for value in classes]
    return {"succeeded": True, "client_version": "9.9.33.51802",
            "truncated": truncated, "node_count_total": len(nodes),
            "topology": {"included": True, "nodes": nodes}}


def test_known_group_marker_rejects_direct_candidate() -> None:
    value = assess_conversation_structure(report("chat-header__contact-name", "group-member-list"),
                                          selector_pack_version="q1")
    assert value.classification == "group"
    assert value.observed_group_markers == ("group-member-list",)


def test_complete_known_private_shell_is_only_a_candidate() -> None:
    value = assess_conversation_structure(
        report("chat-header__contact-name", "ml-root", "ProseMirror", "send-msg"),
        selector_pack_version="q1")
    assert value.classification == "direct_candidate"


@pytest.mark.parametrize("change,code", [
    ({"truncated": True}, "truncated"),
    ({"client_version": "other"}, "version"),
    ({"node_count_total": 99}, "incomplete"),
])
def test_incomplete_or_unbound_reports_fail(change, code) -> None:
    value = report("chat-header__contact-name", "ml-root", "ProseMirror", "send-msg")
    value.update(change)
    with pytest.raises(ConversationStructureError, match=code):
        assess_conversation_structure(value, selector_pack_version="q1")
