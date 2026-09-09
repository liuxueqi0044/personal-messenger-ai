from __future__ import annotations

from messenger_ai.adapters.qq.live_driver.topology import (
    MappingStatus,
    SelectorRole,
    UiNodeInput,
    UiTopologySnapshot,
    compile_selector_pack,
    map_role_candidates,
)

ENV = "a" * 64


def node(
    runtime_id: str,
    control_type: str,
    *,
    parent: str | None = None,
    patterns: tuple[str, ...] = (),
    anchors: tuple[str, ...] = (),
    name: str = "",
    rect: tuple[float, float, float, float] | None = None,
    class_name: str = "",
    automation_id: str | None = None,
) -> UiNodeInput:
    return UiNodeInput(
        runtime_id=runtime_id,
        control_type=control_type,
        parent_runtime_id=parent,
        patterns=patterns,
        semantic_anchors=anchors,
        name=name,
        normalized_rect=rect,
        class_name=class_name,
        automation_id=automation_id,
    )


def fixture(order: tuple[str, ...] | None = None) -> UiTopologySnapshot:
    items = {
        "root": node("root", "Window", anchors=("main_window",)),
        "list": node(
            "list",
            "List",
            parent="root",
            patterns=("SelectionPattern", "ScrollPattern"),
            anchors=("conversation_list",),
            rect=(0.0, 0.1, 0.3, 0.9),
        ),
        "item": node(
            "item",
            "ListItem",
            parent="list",
            patterns=("SelectionItemPattern",),
            anchors=("conversation_item",),
            name="Alice 这是用户数据不应保存",
            rect=(0.0, 0.2, 0.3, 0.3),
        ),
        "messages": node(
            "messages",
            "Document",
            parent="root",
            patterns=("TextPattern", "ScrollPattern"),
            anchors=("message_region",),
            rect=(0.3, 0.1, 0.95, 0.8),
        ),
        "composer": node(
            "composer",
            "Edit",
            parent="root",
            patterns=("ValuePattern", "TextPattern"),
            anchors=("composer",),
            rect=(0.3, 0.8, 0.85, 0.98),
        ),
        "send": node(
            "send",
            "Button",
            parent="root",
            patterns=("InvokePattern",),
            anchors=("send_button",),
            name="发送",
            rect=(0.85, 0.8, 0.98, 0.98),
        ),
    }
    values = [items[key] for key in (order or tuple(items))]
    return UiTopologySnapshot.from_inputs(
        values,
        client_version="9.9.26.44343",
        environment_fingerprint=ENV,
        fixture_suite_version="q1-fixture-v1",
        window_class_name="QQ",
        window_handle=101,
    )


def test_stable_mapping_and_redacted_snapshot() -> None:
    snapshot = fixture()
    result = compile_selector_pack(
        snapshot,
        roles=(
            SelectorRole.MAIN_WINDOW,
            SelectorRole.COMPOSER,
            SelectorRole.SEND_BUTTON,
        ),
    )
    assert result.status is MappingStatus.UNIQUE
    assert result.pack is not None
    assert result.pack.selector(SelectorRole.COMPOSER).node_id
    assert all("Alice" not in repr(item) for item in snapshot.nodes)
    assert all("发送" not in repr(item) for item in snapshot.nodes)


def test_tree_digest_is_stable_under_input_reordering() -> None:
    first = fixture()
    second = fixture(("send", "composer", "messages", "item", "list", "root"))
    assert first.tree_digest == second.tree_digest
    assert tuple(item.node_id for item in first.nodes) == tuple(
        item.node_id for item in second.nodes
    )


def test_list_insertion_does_not_change_existing_role_mapping() -> None:
    before = fixture()
    after = UiTopologySnapshot.from_inputs(
        [
            node("root", "Window", anchors=("main_window",)),
            node(
                "list",
                "List",
                parent="root",
                patterns=("SelectionPattern",),
                anchors=("conversation_list",),
            ),
            node(
                "new-item",
                "ListItem",
                parent="list",
                patterns=("SelectionItemPattern",),
                anchors=("conversation_item",),
            ),
            node(
                "item",
                "ListItem",
                parent="list",
                patterns=("SelectionItemPattern",),
                anchors=("conversation_item",),
            ),
        ],
        client_version="9.9.26.44343",
        environment_fingerprint=ENV,
        fixture_suite_version="q1-fixture-v1",
    )
    before_item = map_role_candidates(before, roles=(SelectorRole.CONVERSATION_ITEM,))[
        0
    ]
    after_item = map_role_candidates(after, roles=(SelectorRole.CONVERSATION_ITEM,))[0]
    assert before_item.status is MappingStatus.UNIQUE
    assert after_item.status is MappingStatus.AMBIGUOUS
    assert before_item.selected_node_id != after_item.selected_node_id


def test_equal_candidates_are_ambiguous_and_not_first_wins() -> None:
    snapshot = UiTopologySnapshot.from_inputs(
        [
            node("root", "Window"),
            node(
                "a",
                "Button",
                parent="root",
                patterns=("InvokePattern",),
                anchors=("send_button",),
            ),
            node(
                "b",
                "Button",
                parent="root",
                patterns=("InvokePattern",),
                anchors=("send_button",),
            ),
        ],
        client_version="v",
        environment_fingerprint=ENV,
        fixture_suite_version="fixture",
    )
    result = compile_selector_pack(snapshot, roles=(SelectorRole.SEND_BUTTON,))
    assert result.status is MappingStatus.AMBIGUOUS
    assert result.pack is None


def test_geometry_alone_cannot_win_and_nickname_is_not_a_signal() -> None:
    snapshot = UiTopologySnapshot.from_inputs(
        [
            node("root", "Window"),
            node("nickname", "Custom", name="发送", rect=(0.85, 0.8, 0.98, 0.98)),
        ],
        client_version="v",
        environment_fingerprint=ENV,
        fixture_suite_version="fixture",
    )
    mapping = map_role_candidates(snapshot, roles=(SelectorRole.SEND_BUTTON,))[0]
    assert mapping.status is MappingStatus.NOT_FOUND


def test_pack_is_bound_to_client_environment_topology_and_fixture() -> None:
    snapshot = fixture()
    result = compile_selector_pack(snapshot, roles=(SelectorRole.COMPOSER,))
    assert result.pack is not None
    assert result.pack.client_version == snapshot.client_version
    assert result.pack.environment_fingerprint == ENV
    assert result.pack.topology_digest != snapshot.tree_digest
    assert result.pack.fixture_suite_version == "q1-fixture-v1"
    assert (
        compile_selector_pack(
            snapshot, client_version="changed", roles=(SelectorRole.COMPOSER,)
        ).status
        is MappingStatus.REJECTED
    )
    changed = fixture(("root", "list", "item", "messages", "composer", "send"))
    assert changed.tree_digest == snapshot.tree_digest
    altered = UiTopologySnapshot.from_inputs(
        [
            node("root", "Window"),
            node(
                "composer",
                "Edit",
                parent="root",
                patterns=("ValuePattern",),
                anchors=("composer",),
            ),
        ],
        client_version=snapshot.client_version,
        environment_fingerprint=ENV,
        fixture_suite_version="other",
    )
    assert altered.tree_digest != snapshot.tree_digest


def _runtime_variant(
    *,
    suffix: str,
    dynamic_message: bool,
) -> UiTopologySnapshot:
    root = f"root-{suffix}"
    message_region = f"messages-{suffix}"
    values = [
        node(root, "Window", anchors=("main_window",), class_name="qq-window"),
        node(
            f"list-{suffix}",
            "List",
            parent=root,
            patterns=("SelectionPattern", "ScrollPattern"),
            anchors=("conversation_list",),
            rect=(0.0, 0.1, 0.3, 0.9),
            automation_id="conversation-list",
        ),
        node(
            f"item-{suffix}",
            "ListItem",
            parent=f"list-{suffix}",
            patterns=("SelectionItemPattern",),
            anchors=("conversation_item",),
            rect=(0.0, 0.2, 0.3, 0.3),
        ),
        node(
            message_region,
            "Document",
            parent=root,
            patterns=("TextPattern", "ScrollPattern"),
            anchors=("message_region",),
            rect=(0.3, 0.1, 0.95, 0.8),
        ),
    ]
    if dynamic_message:
        values.append(
            node(
                f"bubble-{suffix}",
                "Text",
                parent=message_region,
                patterns=("TextPattern",),
                rect=(0.5, 0.5, 0.8, 0.6),
            )
        )
    return UiTopologySnapshot.from_inputs(
        values,
        client_version="9.9.26.44343",
        environment_fingerprint=ENV,
        fixture_suite_version="q1-fixture-v1",
    )


def test_selected_topology_digest_ignores_runtime_ids_and_dynamic_messages() -> None:
    before = _runtime_variant(suffix="before", dynamic_message=False)
    after = _runtime_variant(suffix="after", dynamic_message=True)
    roles = (
        SelectorRole.MAIN_WINDOW,
        SelectorRole.CONVERSATION_LIST,
        SelectorRole.CONVERSATION_ITEM,
        SelectorRole.MESSAGE_REGION,
    )
    first = compile_selector_pack(before, roles=roles).pack
    second = compile_selector_pack(after, roles=roles).pack
    assert first is not None and second is not None
    assert before.tree_digest != after.tree_digest
    assert first.topology_digest == second.topology_digest
    assert (
        first.selector(SelectorRole.MESSAGE_REGION).node_id
        != second.selector(SelectorRole.MESSAGE_REGION).node_id
    )


def test_selected_structural_changes_change_topology_digest() -> None:
    baseline = _runtime_variant(suffix="same", dynamic_message=False)
    altered_inputs = [
        node("root-same", "Window", anchors=("main_window",), class_name="qq-window"),
        node(
            "messages-same",
            "Pane",
            parent="root-same",
            patterns=("TextPattern", "ScrollPattern"),
            anchors=("message_region",),
            rect=(0.32, 0.1, 0.95, 0.8),
        ),
    ]
    altered = UiTopologySnapshot.from_inputs(
        altered_inputs,
        client_version=baseline.client_version,
        environment_fingerprint=baseline.environment_fingerprint,
        fixture_suite_version=baseline.fixture_suite_version,
    )
    first = compile_selector_pack(baseline, roles=(SelectorRole.MESSAGE_REGION,)).pack
    second = compile_selector_pack(altered, roles=(SelectorRole.MESSAGE_REGION,)).pack
    assert first is not None and second is not None
    assert first.topology_digest != second.topology_digest
