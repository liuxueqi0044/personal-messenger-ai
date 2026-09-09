"""Strict bridge from the read-only QQ probe JSON into Q0/Q1 contracts."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .environment import ModalState, RuntimeSnapshot, WindowBounds, WindowPresentation
from .topology import UiNodeInput, UiTopologySnapshot


class ProbeBridgeError(ValueError):
    """Safe, stable parse error; never includes probe text or user content."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_HOST = (
    "executable_path",
    "executable_signature",
    "process_signature",
    "client_version",
    "windows_version",
    "dpi_scale",
    "monitor_id",
    "monitor_topology_digest",
    "theme",
    "window_class",
    "is_occluded",
    "is_logged_in",
    "modal_state",
)


def _object(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProbeBridgeError("INVALID_SHAPE", f"{label} must be an object")
    return value


def _required(mapping: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in mapping:
        raise ProbeBridgeError("MISSING_FIELD", f"{label}.{key} is required")
    return mapping[key]


def _bool(mapping: Mapping[str, Any], key: str, label: str) -> bool:
    value = _required(mapping, key, label)
    if not isinstance(value, bool):
        raise ProbeBridgeError("INVALID_FIELD", f"{label}.{key} must be boolean")
    return value


def _number(mapping: Mapping[str, Any], key: str, label: str) -> float:
    value = _required(mapping, key, label)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ProbeBridgeError("INVALID_FIELD", f"{label}.{key} must be finite numeric")
    return float(value)


def _text(mapping: Mapping[str, Any], key: str, label: str) -> str:
    value = _required(mapping, key, label)
    if not isinstance(value, str) or not value.strip():
        raise ProbeBridgeError("INVALID_FIELD", f"{label}.{key} must be non-empty text")
    return value


def _parse_input(report: Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(report, Mapping):
        return report
    if isinstance(report, (str, bytes, bytearray)):
        try:
            decoded = json.loads(report)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ProbeBridgeError(
                "INVALID_JSON", "probe report is not valid JSON"
            ) from exc
        return _object(decoded, "report")
    raise ProbeBridgeError("INVALID_SHAPE", "probe report must be an object or JSON")


def _presentation(
    host: Mapping[str, Any], bounds: Mapping[str, Any]
) -> WindowPresentation:
    if "presentation" in host:
        value = _text(host, "presentation", "host_environment").lower()
        try:
            return WindowPresentation(value)
        except ValueError as exc:
            raise ProbeBridgeError(
                "UNKNOWN_WINDOW_STATE", "unsupported presentation"
            ) from exc
    minimized = _bool(bounds, "minimized", "window_bounds")
    maximized = _bool(bounds, "maximized", "window_bounds")
    if minimized:
        return WindowPresentation.MINIMIZED
    if maximized:
        return WindowPresentation.MAXIMIZED
    return WindowPresentation.NORMAL


def _modal(value: object) -> ModalState:
    if not isinstance(value, str):
        raise ProbeBridgeError("UNKNOWN_MODAL_STATE", "modal_state must be text")
    try:
        return ModalState(value.lower())
    except ValueError as exc:
        raise ProbeBridgeError(
            "UNKNOWN_MODAL_STATE", "unsupported modal state"
        ) from exc


def _bounds(bounds: Mapping[str, Any]) -> WindowBounds:
    x = _number(bounds, "x", "window_bounds")
    y = _number(bounds, "y", "window_bounds")
    width = _number(bounds, "width", "window_bounds")
    height = _number(bounds, "height", "window_bounds")
    if width <= 0 or height <= 0:
        raise ProbeBridgeError(
            "INVALID_BOUNDS", "window bounds must have positive area"
        )
    values = (x, y, x + width, y + height)
    if any(
        not math.isfinite(item) or item < -2_147_483_648 or item > 2_147_483_647
        for item in values
    ):
        raise ProbeBridgeError(
            "INVALID_BOUNDS", "window bounds are outside integer range"
        )
    return WindowBounds(
        left=round(x), top=round(y), right=round(x + width), bottom=round(y + height)
    )


def _normalized_bounds(
    node: Mapping[str, Any], index: int
) -> tuple[float, float, float, float] | None:
    value = _required(node, "normalized_bounds", f"topology.nodes[{index}]")
    if value is None:
        return None
    raw = _object(value, "normalized_bounds")
    x = _number(raw, "x", "normalized_bounds")
    y = _number(raw, "y", "normalized_bounds")
    width = _number(raw, "width", "normalized_bounds")
    height = _number(raw, "height", "normalized_bounds")
    if width <= 0 or height <= 0 or x < 0 or y < 0 or x + width > 1 or y + height > 1:
        raise ProbeBridgeError(
            "INVALID_NODE_BOUNDS", "normalized node bounds are outside the root"
        )
    return x, y, x + width, y + height


def _structural_q1_anchors(
    nodes: list[UiNodeInput],
) -> dict[str, str]:
    """Recognize QQ NT's Chromium split panes without visible text.

    QQ 9.9.26 exposes its virtualized conversation list as nested ``Window``
    and ``Custom`` controls instead of UIA ``List``/``ListItem`` controls.  A
    maximized chat shell still has a strong, text-independent signature: one
    narrow left window, one wide right window, and at least three equal-sized
    repeated rows directly under the left window.  Returning nothing on any
    ambiguity keeps the generic/fixture path fail-closed.
    """

    def dimensions(item: UiNodeInput) -> tuple[float, float, float, float] | None:
        if item.normalized_rect is None:
            return None
        left, top, right, bottom = item.normalized_rect
        return left, top, right - left, bottom - top

    windows = [
        item
        for item in nodes
        if item.control_type.lower() == "window" and dimensions(item) is not None
    ]
    left_windows = []
    right_windows = []
    for item in windows:
        rect = dimensions(item)
        assert rect is not None
        left, top, width, height = rect
        if (
            0.03 <= left <= 0.14
            and 0.08 <= top <= 0.20
            and 0.15 <= width <= 0.30
            and height >= 0.75
        ):
            left_windows.append(item)
        if (
            0.20 <= left <= 0.40
            and 0.08 <= top <= 0.20
            and width >= 0.55
            and height >= 0.75
        ):
            right_windows.append(item)
    if len(left_windows) != 1 or len(right_windows) != 1:
        return {}

    conversation_list = left_windows[0]
    message_region = right_windows[0]
    list_rect = dimensions(conversation_list)
    region_rect = dimensions(message_region)
    assert list_rect is not None and region_rect is not None
    list_left, list_top, list_width, _ = list_rect
    region_left, region_top, _, _ = region_rect
    if abs(list_top - region_top) > 0.03 or region_left < list_left + list_width - 0.03:
        return {}

    row_candidates: list[UiNodeInput] = []
    for item in nodes:
        rect = dimensions(item)
        if rect is None:
            continue
        left, top, width, height = rect
        if (
            item.parent_runtime_id == conversation_list.runtime_id
            and item.control_type.lower() == "custom"
            and "invokepattern" in {pattern.lower() for pattern in item.patterns}
            and abs(left - list_left) <= 0.02
            and abs(width - list_width) <= 0.02
            and list_top <= top < 0.94
            and 0.06 <= height <= 0.14
        ):
            row_candidates.append(item)
    groups: dict[tuple[float, float], list[UiNodeInput]] = {}
    for item in row_candidates:
        rect = dimensions(item)
        assert rect is not None
        groups.setdefault((round(rect[2], 2), round(rect[3], 2)), []).append(item)
    repeated = [group for group in groups.values() if len(group) >= 3]
    if len(repeated) != 1:
        return {}
    exemplar = min(repeated[0], key=lambda item: dimensions(item)[1])  # type: ignore[index]
    return {
        conversation_list.runtime_id: "conversation_list",
        exemplar.runtime_id: "conversation_item",
        message_region.runtime_id: "message_region",
    }


def _enrich_q1_anchors(nodes: list[UiNodeInput]) -> list[UiNodeInput]:
    derived = _structural_q1_anchors(nodes)
    if not derived:
        return nodes
    structural_roles = {"conversation_list", "conversation_item", "message_region"}
    enriched: list[UiNodeInput] = []
    for item in nodes:
        anchors = {
            anchor
            for anchor in item.semantic_anchors
            if anchor.lower() not in structural_roles
        }
        role = derived.get(item.runtime_id)
        if role is not None:
            anchors.add(role)
        enriched.append(replace(item, semantic_anchors=tuple(sorted(anchors))))
    return enriched


def _node_inputs(
    topology: Mapping[str, Any], root_runtime_id: str
) -> list[UiNodeInput]:
    if _bool(topology, "included", "topology") is not True:
        raise ProbeBridgeError("TOPOLOGY_MISSING", "topology.included must be true")
    digest = _text(topology, "digest", "topology").lower()
    if not _HEX64.fullmatch(digest):
        raise ProbeBridgeError(
            "INVALID_TOPOLOGY_DIGEST", "topology.digest must be SHA-256"
        )
    values = _required(topology, "nodes", "topology")
    if not isinstance(values, list) or not values:
        raise ProbeBridgeError(
            "TOPOLOGY_MISSING", "topology.nodes must be a non-empty array"
        )
    nodes: list[UiNodeInput] = []
    seen: set[str] = set()
    for index, item in enumerate(values):
        node = _object(item, f"topology.nodes[{index}]")
        runtime_id = _text(node, "runtime_id", f"topology.nodes[{index}]")
        if runtime_id in seen:
            raise ProbeBridgeError(
                "DUPLICATE_NODE", "topology runtime ids must be unique"
            )
        seen.add(runtime_id)
        parent = _required(node, "parent_runtime_id", f"topology.nodes[{index}]")
        if parent is not None and (not isinstance(parent, str) or not parent):
            raise ProbeBridgeError(
                "INVALID_FIELD", "parent_runtime_id must be text or null"
            )
        control_type = _text(node, "control_type", f"topology.nodes[{index}]")
        if control_type.lower().startswith("controltype."):
            control_type = control_type.split(".", 1)[1]
        class_name = _required(node, "class_name", f"topology.nodes[{index}]")
        if not isinstance(class_name, str):
            raise ProbeBridgeError("INVALID_FIELD", "class_name must be text")
        automation_id = _required(node, "automation_id", f"topology.nodes[{index}]")
        if automation_id is not None and not isinstance(automation_id, str):
            raise ProbeBridgeError(
                "INVALID_FIELD", "automation_id must be text or null"
            )
        patterns = _required(node, "patterns", f"topology.nodes[{index}]")
        anchors = _required(node, "semantic_anchors", f"topology.nodes[{index}]")
        if not isinstance(patterns, list) or any(
            not isinstance(item, str) for item in patterns
        ):
            raise ProbeBridgeError("INVALID_FIELD", "patterns must be a string array")
        if not isinstance(anchors, list) or any(
            not isinstance(item, str) for item in anchors
        ):
            raise ProbeBridgeError(
                "INVALID_FIELD", "semantic_anchors must be a string array"
            )
        nodes.append(
            UiNodeInput(
                runtime_id=runtime_id,
                parent_runtime_id=parent or root_runtime_id,
                control_type=control_type,
                class_name=class_name,
                automation_id=automation_id,
                patterns=tuple(patterns),
                semantic_anchors=tuple(anchors),
                normalized_rect=_normalized_bounds(node, index),
            )
        )
    if root_runtime_id in seen:
        raise ProbeBridgeError(
            "DUPLICATE_NODE", "probe root runtime id collides with a node"
        )
    known_ids = seen | {root_runtime_id}
    if any(item.parent_runtime_id not in known_ids for item in nodes):
        raise ProbeBridgeError("INVALID_PARENT", "topology parent reference is unknown")
    return _enrich_q1_anchors(nodes)


def ingest_probe_report(
    report: Mapping[str, Any] | str | bytes,
    fixture_suite_version: str,
) -> tuple[RuntimeSnapshot, UiTopologySnapshot]:
    """Validate a probe report and return only Q0/Q1 sanitized snapshots."""
    if not isinstance(fixture_suite_version, str) or not fixture_suite_version.strip():
        raise ProbeBridgeError(
            "INVALID_FIXTURE_VERSION", "fixture suite version is required"
        )
    value = _parse_input(report)
    if (
        _bool(value, "succeeded", "report") is not True
        or _bool(value, "read_only", "report") is not True
    ):
        raise ProbeBridgeError(
            "PROBE_NOT_CERTIFIED", "probe must succeed in read-only mode"
        )
    privacy = _object(_required(value, "privacy", "report"), "privacy")
    for key in (
        "emitted_control_names",
        "emitted_message_text",
        "changed_window_state",
    ):
        if _bool(privacy, key, "privacy") is not False:
            raise ProbeBridgeError(
                "PRIVACY_VIOLATION", "probe privacy flags are not clean"
            )
    host = _object(_required(value, "host_environment", "report"), "host_environment")
    for key in _REQUIRED_HOST:
        _required(host, key, "host_environment")
    bounds_value = _object(_required(value, "window_bounds", "report"), "window_bounds")
    bounds = _bounds(bounds_value)
    presentation = _presentation(host, bounds_value)
    is_foreground = _bool(bounds_value, "foreground", "window_bounds")
    # Geometry usability is a Q0 runtime fact, not a parse gate: minimized or
    # normal windows must still become snapshots so Q0 can reject them with a
    # precise state reason. Node geometry itself remains strictly bounded.
    _bool(bounds_value, "geometry_usable", "window_bounds")
    process_id = _required(value, "process_id", "report")
    window_handle = _required(value, "window_handle", "report")
    if (
        isinstance(process_id, bool)
        or not isinstance(process_id, int)
        or process_id <= 0
    ):
        raise ProbeBridgeError("INVALID_METADATA", "process_id must be positive")
    if (
        isinstance(window_handle, bool)
        or not isinstance(window_handle, int)
        or window_handle <= 0
    ):
        raise ProbeBridgeError("INVALID_METADATA", "window_handle must be positive")
    executable_signature = _text(
        host, "executable_signature", "host_environment"
    ).lower()
    process_signature = _text(host, "process_signature", "host_environment").lower()
    monitor_digest = _text(host, "monitor_topology_digest", "host_environment").lower()
    if not all(
        _HEX64.fullmatch(item)
        for item in (executable_signature, process_signature, monitor_digest)
    ):
        raise ProbeBridgeError(
            "INVALID_METADATA", "environment signatures must be SHA-256"
        )
    runtime = RuntimeSnapshot(
        process_id=process_id,
        window_handle=window_handle,
        executable_path=_text(host, "executable_path", "host_environment"),
        executable_signature=executable_signature,
        process_signature=process_signature,
        client_version=_text(host, "client_version", "host_environment"),
        windows_version=_text(host, "windows_version", "host_environment"),
        dpi_scale=_number(host, "dpi_scale", "host_environment"),
        monitor_id=_text(host, "monitor_id", "host_environment"),
        monitor_topology_digest=monitor_digest,
        theme=_text(host, "theme", "host_environment"),
        window_class=_text(host, "window_class", "host_environment"),
        presentation=presentation,
        bounds=bounds,
        is_foreground=is_foreground,
        is_occluded=_bool(host, "is_occluded", "host_environment"),
        is_logged_in=_bool(host, "is_logged_in", "host_environment"),
        modal_state=_modal(_required(host, "modal_state", "host_environment")),
    )
    root = _object(_required(value, "root", "report"), "root")
    root_control = _text(root, "control_type", "root")
    root_class = root.get("class_name", "")
    if not isinstance(root_class, str):
        raise ProbeBridgeError("INVALID_FIELD", "root.class_name must be text")
    root_runtime_id = "__probe_root__"
    topology = _object(_required(value, "topology", "report"), "topology")
    nodes = _node_inputs(topology, root_runtime_id)
    nodes.insert(
        0,
        UiNodeInput(
            runtime_id=root_runtime_id,
            control_type=root_control,
            class_name=root_class,
            semantic_anchors=("main_window",),
            normalized_rect=(0.0, 0.0, 1.0, 1.0),
        ),
    )
    sanitized = UiTopologySnapshot.from_inputs(
        nodes,
        client_version=runtime.client_version,
        environment_fingerprint=runtime.fingerprint.digest,
        fixture_suite_version=fixture_suite_version,
        window_class_name=runtime.window_class,
        window_handle=runtime.window_handle,
    )
    return runtime, sanitized


__all__ = ["ProbeBridgeError", "ingest_probe_report"]
