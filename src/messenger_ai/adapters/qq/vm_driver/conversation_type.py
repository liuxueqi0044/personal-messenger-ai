"""Version-bound structural screening; this module does not certify identity."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping


SUPPORTED_CLIENT_VERSION = "9.9.33.51802"
GROUP_MARKERS = frozenset({
    "group-box__toggle",
    "group-user",
    "group-notice",
    "group-member-list",
})
DIRECT_SHELL_MARKERS = frozenset({
    "chat-header__contact-name",
    "ml-root",
    "ProseMirror",
    "send-msg",
})


class ConversationStructureError(ValueError):
    pass


@dataclass(frozen=True)
class StructuralConversationAssessment:
    classification: Literal["group", "direct_candidate"]
    client_version: str
    selector_pack_version: str
    observed_group_markers: tuple[str, ...]


def assess_conversation_structure(
    report: Mapping[str, Any], *, selector_pack_version: str,
    expected_client_version: str = SUPPORTED_CLIENT_VERSION,
) -> StructuralConversationAssessment:
    """Screen a complete structural report; ``direct_candidate`` is not proof."""
    if report.get("succeeded") is not True:
        raise ConversationStructureError("structure_capture_failed")
    if report.get("client_version") != expected_client_version:
        raise ConversationStructureError("structure_client_version_mismatch")
    if report.get("truncated") is not False:
        raise ConversationStructureError("structure_tree_truncated")
    topology = report.get("topology")
    if not isinstance(topology, Mapping) or topology.get("included") is not True:
        raise ConversationStructureError("structure_topology_missing")
    nodes = topology.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ConversationStructureError("structure_nodes_missing")
    if report.get("node_count_total") != len(nodes):
        raise ConversationStructureError("structure_nodes_incomplete")
    tokens: set[str] = set()
    for node in nodes:
        if not isinstance(node, Mapping):
            raise ConversationStructureError("structure_node_invalid")
        tokens.update(str(node.get("class_name", "")).split())
    groups = tuple(sorted(tokens & GROUP_MARKERS))
    if groups:
        return StructuralConversationAssessment(
            "group", expected_client_version, selector_pack_version, groups
        )
    missing = DIRECT_SHELL_MARKERS - tokens
    if missing:
        raise ConversationStructureError("structure_layout_unknown")
    return StructuralConversationAssessment(
        "direct_candidate", expected_client_version, selector_pack_version, ()
    )
