from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from messenger_ai.adapters.qq.live_driver.environment import (
    CertifiedQQProfile,
    QQEnvironmentSentinel,
    WindowPresentation,
)
from messenger_ai.adapters.qq.live_driver.probe_bridge import (
    ProbeBridgeError,
    ingest_probe_report,
)
from messenger_ai.adapters.qq.live_driver.topology import (
    MappingStatus,
    SelectorRole,
    compile_selector_pack,
)

HASH = "a" * 64
REAL_QQ_9933_EMPTY = (
    Path(__file__).with_name("fixtures") / "qq_9_9_33_foreground_empty.json"
)
REAL_QQ_9933_SELECTED_GROUP = (
    Path(__file__).with_name("fixtures")
    / "qq_9_9_33_foreground_selected_group.json"
)


def report() -> dict:
    return {
        "probe_version": "qq-uia-readonly-v1",
        "succeeded": True,
        "read_only": True,
        "process_id": 1234,
        "window_handle": 5678,
        "host_environment": {
            "executable_path": "C:/Program Files/Tencent/QQ.exe",
            "executable_signature": HASH,
            "process_signature": HASH,
            "client_version": "9.9.26.44343",
            "windows_version": "Windows 11",
            "dpi_scale": 1.0,
            "monitor_id": "monitor-1",
            "monitor_topology_digest": HASH,
            "theme": "light",
            "window_class": "QQ",
            "is_occluded": True,
            "is_logged_in": True,
            "modal_state": "none",
        },
        "window_bounds": {
            "x": 0,
            "y": 0,
            "width": 1920,
            "height": 1080,
            "minimized": False,
            "maximized": True,
            "foreground": False,
            "geometry_usable": True,
        },
        "root": {"control_type": "Window", "class_name": "QQMain"},
        "topology": {
            "included": True,
            "digest": HASH,
            "nodes": [
                {
                    "runtime_id": "composer-runtime",
                    "parent_runtime_id": None,
                    "control_type": "Edit",
                    "class_name": "RichEdit",
                    "automation_id": "composer-aid",
                    "patterns": ["valuepattern", "textpattern"],
                    "semantic_anchors": ["composer"],
                    "normalized_bounds": {
                        "x": 0.3,
                        "y": 0.8,
                        "width": 0.5,
                        "height": 0.1,
                    },
                    "name": "秘密联系人姓名，不得输出",
                }
            ],
        },
        "privacy": {
            "emitted_control_names": False,
            "emitted_message_text": False,
            "changed_window_state": False,
        },
    }


def test_successfully_bridges_q0_and_q1_without_names() -> None:
    runtime, topology = ingest_probe_report(report(), "q1-fixture-v1")
    assert runtime.presentation is WindowPresentation.MAXIMIZED
    assert runtime.bounds.left == 0 and runtime.bounds.right == 1920
    assert runtime.is_logged_in and runtime.is_occluded
    assert topology.client_version == runtime.client_version
    assert topology.fixture_suite_version == "q1-fixture-v1"
    assert any("秘密联系人姓名" not in repr(node) for node in topology.nodes)
    assert "秘密联系人姓名" not in repr(topology)
    assert any("main_window" in node.semantic_anchors for node in topology.nodes)
    composer = next(
        node for node in topology.nodes if "composer" in node.semantic_anchors
    )
    assert composer.normalized_rect == (0.3, 0.8, 0.8, 0.9)


def test_privacy_flags_are_required_and_must_be_clean() -> None:
    value = report()
    value["privacy"]["emitted_message_text"] = True
    with pytest.raises(ProbeBridgeError, match="PRIVACY_VIOLATION"):
        ingest_probe_report(value, "fixture")
    value = report()
    del value["privacy"]["changed_window_state"]
    with pytest.raises(ProbeBridgeError, match="MISSING_FIELD"):
        ingest_probe_report(value, "fixture")


def test_minimized_and_normal_windows_are_constructed_but_q0_rejects() -> None:
    for minimized, maximized, expected in (
        (True, False, WindowPresentation.MINIMIZED),
        (False, False, WindowPresentation.NORMAL),
    ):
        value = report()
        value["window_bounds"]["minimized"] = minimized
        value["window_bounds"]["maximized"] = maximized
        if minimized:
            value["window_bounds"]["geometry_usable"] = False
        runtime, _ = ingest_probe_report(value, "fixture")
        assert runtime.presentation is expected
        profile = CertifiedQQProfile(
            profile_id="fixture-profile",
            allowed_executable_paths=(runtime.executable_path,),
            allowed_executable_signatures=(runtime.executable_signature,),
            allowed_process_signatures=(runtime.process_signature,),
            allowed_window_classes=(runtime.window_class,),
            certified_fingerprint=runtime.fingerprint,
        )
        envelope = QQEnvironmentSentinel(object(), profile).assess(runtime)
        assert not envelope.decision.commit_allowed


@pytest.mark.parametrize(
    "mutator,code",
    [
        (lambda value: value["topology"].pop("nodes"), "MISSING_FIELD"),
        (lambda value: value["topology"].update(included=False), "TOPOLOGY_MISSING"),
        (
            lambda value: value["topology"].update(digest="bad"),
            "INVALID_TOPOLOGY_DIGEST",
        ),
        (lambda value: value["window_bounds"].update(width=0), "INVALID_BOUNDS"),
        (
            lambda value: value["topology"]["nodes"][0]["normalized_bounds"].update(
                x=0.9
            ),
            "INVALID_NODE_BOUNDS",
        ),
    ],
)
def test_missing_or_malicious_probe_data_fails_closed(mutator, code: str) -> None:
    value = copy.deepcopy(report())
    mutator(value)
    with pytest.raises(ProbeBridgeError, match=code):
        ingest_probe_report(value, "fixture")


def test_missing_host_login_modal_or_window_state_is_not_guessed() -> None:
    for path in (
        ("host_environment", "is_logged_in"),
        ("host_environment", "modal_state"),
        ("window_bounds", "maximized"),
    ):
        value = copy.deepcopy(report())
        del value[path[0]][path[1]]
        with pytest.raises(ProbeBridgeError, match="MISSING_FIELD"):
            ingest_probe_report(value, "fixture")


def test_offscreen_node_without_geometry_is_preserved_as_structural_only() -> None:
    value = report()
    value["topology"]["nodes"][0]["normalized_bounds"] = None

    _runtime, topology = ingest_probe_report(value, "fixture")

    composer = next(
        node for node in topology.nodes if "composer" in node.semantic_anchors
    )
    assert composer.normalized_rect is None


def test_dotnet_control_type_prefix_is_normalized() -> None:
    value = report()
    value["topology"]["nodes"][0]["control_type"] = "ControlType.Edit"

    _runtime, topology = ingest_probe_report(value, "fixture")

    composer = next(
        node for node in topology.nodes if "composer" in node.semantic_anchors
    )
    assert composer.control_type == "edit"


def test_qq_nt_chromium_split_panes_are_mapped_without_visible_text() -> None:
    value = report()
    value["topology"]["nodes"] = [
        {
            "runtime_id": "left-window",
            "parent_runtime_id": "shell",
            "control_type": "ControlType.Window",
            "class_name": "",
            "automation_id": "",
            "patterns": ["invokepattern"],
            "semantic_anchors": [],
            "normalized_bounds": {
                "x": 0.05,
                "y": 0.14,
                "width": 0.22,
                "height": 0.86,
            },
        },
        {
            "runtime_id": "right-window",
            "parent_runtime_id": "shell",
            "control_type": "ControlType.Window",
            "class_name": "",
            "automation_id": "",
            "patterns": ["invokepattern"],
            "semantic_anchors": [],
            "normalized_bounds": {
                "x": 0.27,
                "y": 0.14,
                "width": 0.73,
                "height": 0.86,
            },
        },
        {
            "runtime_id": "shell",
            "parent_runtime_id": "__probe_root__",
            "control_type": "ControlType.Custom",
            "class_name": "",
            "automation_id": "",
            "patterns": ["invokepattern"],
            "semantic_anchors": [],
            "normalized_bounds": {
                "x": 0.05,
                "y": 0.06,
                "width": 0.95,
                "height": 0.94,
            },
        },
    ]
    for index, top in enumerate((0.16, 0.25, 0.34, 0.43)):
        value["topology"]["nodes"].append(
            {
                "runtime_id": f"row-{index}",
                "parent_runtime_id": "left-window",
                "control_type": "ControlType.Custom",
                "class_name": "",
                "automation_id": "",
                "patterns": ["invokepattern"],
                # Simulate the old broad local heuristic.  The bridge must
                # retain one structural exemplar instead of four candidates.
                "semantic_anchors": ["conversation_item"],
                "normalized_bounds": {
                    "x": 0.05,
                    "y": top,
                    "width": 0.22,
                    "height": 0.09,
                },
            }
        )

    _runtime, topology = ingest_probe_report(value, "fixture")
    result = compile_selector_pack(
        topology,
        roles=(
            SelectorRole.CONVERSATION_LIST,
            SelectorRole.CONVERSATION_ITEM,
            SelectorRole.MESSAGE_REGION,
        ),
    )

    assert result.status is MappingStatus.UNIQUE
    assert result.pack is not None
    assert {item.role for item in result.pack.selectors} == {
        SelectorRole.CONVERSATION_LIST,
        SelectorRole.CONVERSATION_ITEM,
        SelectorRole.MESSAGE_REGION,
    }


def test_qq_9933_real_empty_shell_maps_list_and_rows_but_not_messages() -> None:
    value = json.loads(REAL_QQ_9933_EMPTY.read_text(encoding="utf-8-sig"))
    _runtime, topology = ingest_probe_report(value, "qq-uia-readonly-v2")

    result = compile_selector_pack(
        topology,
        client_version="9.9.33.51802",
        roles=(SelectorRole.CONVERSATION_LIST, SelectorRole.CONVERSATION_ITEM),
    )
    assert result.status is MappingStatus.UNIQUE
    assert result.pack is not None
    selectors = {item.role: item for item in result.pack.selectors}
    assert selectors[SelectorRole.CONVERSATION_LIST].control_type == "pane"
    assert "recent-contact-list" in selectors[SelectorRole.CONVERSATION_LIST].class_name
    assert selectors[SelectorRole.CONVERSATION_LIST].patterns == ()
    assert selectors[SelectorRole.CONVERSATION_LIST].automation_id_digest is None
    assert selectors[SelectorRole.CONVERSATION_ITEM].control_type == "group"
    assert selectors[SelectorRole.CONVERSATION_ITEM].class_name == "recent-contact-item"
    assert selectors[SelectorRole.CONVERSATION_ITEM].patterns == ("invokepattern",)
    assert selectors[SelectorRole.CONVERSATION_ITEM].automation_id_digest is None

    message = compile_selector_pack(
        topology, roles=(SelectorRole.MESSAGE_REGION,)
    )
    assert message.status is MappingStatus.NOT_FOUND
    assert message.pack is None

    wrong_version = compile_selector_pack(
        topology,
        client_version="9.9.26.44343",
        roles=(SelectorRole.CONVERSATION_LIST,),
    )
    assert wrong_version.status is MappingStatus.REJECTED


def test_qq_9933_duplicate_recent_lists_fail_closed() -> None:
    value = json.loads(REAL_QQ_9933_EMPTY.read_text(encoding="utf-8-sig"))
    original = next(
        node
        for node in value["topology"]["nodes"]
        if "recent-contact-list" in node["class_name"].split()
    )
    duplicate = copy.deepcopy(original)
    duplicate["runtime_id"] = "duplicate-recent-contact-list"
    duplicate["structural_id"] = "b" * 64
    value["topology"]["nodes"].append(duplicate)

    _runtime, topology = ingest_probe_report(value, "qq-uia-readonly-v2")
    result = compile_selector_pack(
        topology,
        roles=(SelectorRole.CONVERSATION_LIST, SelectorRole.CONVERSATION_ITEM),
    )

    assert result.status is MappingStatus.NOT_FOUND
    assert result.pack is None


def test_qq_9933_real_selected_group_maps_inner_message_scroller() -> None:
    value = json.loads(REAL_QQ_9933_SELECTED_GROUP.read_text(encoding="utf-8-sig"))
    _runtime, topology = ingest_probe_report(value, "qq-uia-readonly-v2")

    result = compile_selector_pack(
        topology,
        roles=(SelectorRole.MESSAGE_REGION,),
    )

    assert result.status is MappingStatus.UNIQUE
    assert result.pack is not None
    selector = result.pack.selector(SelectorRole.MESSAGE_REGION)
    assert selector.control_type == "group"
    assert {"q-scroll-view", "ml-container", "ml-root", "container"} <= set(
        selector.class_name.split()
    )
    assert selector.patterns == ("invokepattern", "scrollpattern")
    assert sum(
        "message_region" in node.semantic_anchors for node in topology.nodes
    ) == 1
