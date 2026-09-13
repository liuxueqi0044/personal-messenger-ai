"""Decode observed QQ 9.9.33 message rows without persistent RuntimeId identity."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from messenger_ai.adapters.qq.models import BubbleDirection


class MessageDecodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class DecodedMessage:
    direction: BubbleDirection
    text: str
    message_key: str


def _tokens(node: Any) -> set[str]:
    return set(str(getattr(node, "ClassName", "")).split())


def _descendants(node: Any, *, max_depth: int = 8):
    queue = [(child, 1) for child in node.GetChildren()]
    while queue:
        child, depth = queue.pop(0)
        yield child
        if depth < max_depth:
            queue.extend((grandchild, depth + 1) for grandchild in child.GetChildren())


def _control_type(node: Any) -> str:
    value = str(getattr(node, "ControlTypeName", "")).lower()
    return value.replace("controltype.", "").removesuffix("control")


def _leaf_text(node: Any) -> str | None:
    if node.GetChildren() or _control_type(node) != "text":
        return None
    name = str(getattr(node, "Name", ""))
    if name:
        return name
    pattern = node.GetTextPattern()
    if pattern is None:
        return None
    value = str(pattern.DocumentRange.GetText(-1))
    return value[:-1] if value.endswith("\r") else value


def decode_message_region(region: Any) -> tuple[DecodedMessage, ...]:
    if "ml-root" not in _tokens(region):
        raise MessageDecodeError("message_region_not_ml_root")
    children = list(region.GetChildren())
    if not children:
        rows = []
    elif all(_control_type(item) == "group" and "message" in _tokens(item)
             for item in children):
        # Older flattened UIA snapshots expose message rows directly.
        rows = children
    elif (len(children) == 1 and _control_type(children[0]) == "group"
          and {"ml-list", "list"}.issubset(_tokens(children[0]))):
        # QQ 9.9.33 Python UIA exposes ml-root -> ml-list -> ml-item -> message.
        rows = []
        for wrapper in children[0].GetChildren():
            nested = list(wrapper.GetChildren())
            if (_control_type(wrapper) != "group" or "ml-item" not in _tokens(wrapper)
                    or len(nested) != 1 or _control_type(nested[0]) != "group"
                    or "message" not in _tokens(nested[0])):
                raise MessageDecodeError("message_list_wrapper_unsupported")
            rows.append(nested[0])
    else:
        raise MessageDecodeError("message_region_children_unsupported")
    decoded = []
    for row in rows:
        if _control_type(row) != "group" or "message" not in _tokens(row):
            raise MessageDecodeError("message_row_unsupported")
        descendants = list(_descendants(row, max_depth=8))
        if any("message" in _tokens(node) for node in descendants):
            raise MessageDecodeError("nested_message_row_unsupported")
        gray = [node for node in descendants if "gray-tip-message" in _tokens(node)]
        if gray:
            if len(gray) != 1 or any("msg-content-container" in _tokens(node) for node in descendants):
                raise MessageDecodeError("gray_tip_structure_ambiguous")
            continue
        content = [node for node in descendants
                   if "msg-content-container" in _tokens(node)]
        if len(content) != 1:
            raise MessageDecodeError("message_content_container_ambiguous")
        tokens = _tokens(content[0])
        directions = tokens & {"container--self", "container--others"}
        if len(directions) != 1:
            raise MessageDecodeError("message_direction_ambiguous")
        body_nodes = list(_descendants(content[0], max_depth=6))
        if any(_control_type(node) not in {"group", "text"} for node in body_nodes):
            raise MessageDecodeError("message_content_unsupported")
        leaves = [value for node in body_nodes
                  if (value := _leaf_text(node)) is not None]
        if not leaves or any(value == "" for value in leaves):
            raise MessageDecodeError("message_content_unsupported")
        text = "\n".join(leaves)
        direction = (BubbleDirection.OUTBOUND if "container--self" in directions
                     else BubbleDirection.INBOUND)
        key = hashlib.sha256(f"qq-visible-v1|{direction.value}|{text}".encode()).hexdigest()
        decoded.append(DecodedMessage(direction, text, key))
    return tuple(decoded)
