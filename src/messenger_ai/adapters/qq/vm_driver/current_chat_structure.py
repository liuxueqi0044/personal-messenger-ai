"""Value-only structure shared by navigation and current-chat execution.

QQ's normal profile window can blur the editor without replacing it. The
composer's exact ProseMirror-focused and is-empty tokens describe focus and
content state, not the control instance. Real focus and empty/owned contents
are checked independently at every input boundary; neither is inferred here.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence


_COMPOSER_STATE = re.compile(r"(?<!\S)(?:ProseMirror-focused|is-empty)(?!\S)")
ControlStructure = tuple[str, Sequence[int]]


def _composer_class(class_name: str) -> str:
    # Remove only the exact token and one adjacent separator. Do not sort
    # classes, normalize unrelated whitespace, or ignore other state tokens.
    for match in reversed(tuple(_COMPOSER_STATE.finditer(class_name))):
        start, end = match.span()
        if start and class_name[start - 1].isspace():
            start -= 1
        elif end < len(class_name) and class_name[end].isspace():
            end += 1
        class_name = class_name[:start] + class_name[end:]
    return class_name


def current_chat_structure_projection(*, header: ControlStructure,
                                      messages: ControlStructure,
                                      composer: ControlStructure) -> tuple[tuple[str, tuple[int, ...]], ...]:
    """Retain role order, all RuntimeIds and every other ClassName token."""
    return ((header[0], tuple(header[1])), (messages[0], tuple(messages[1])),
            (_composer_class(composer[0]), tuple(composer[1])))


def current_chat_structure_digest(*, header: ControlStructure,
                                  messages: ControlStructure,
                                  composer: ControlStructure) -> str:
    projection = current_chat_structure_projection(header=header, messages=messages, composer=composer)
    return hashlib.sha256(json.dumps(projection, separators=(",", ":")).encode("utf-8")).hexdigest()
