import hashlib
import json

import pytest

from messenger_ai.adapters.qq.vm_driver.current_chat_structure import (
    current_chat_structure_digest, current_chat_structure_projection,
)


def shape():
    return dict(header=("chat-header__contact-name", [42, 101]),
                messages=("q-scroll-view scroll-view--hide-scrollbar ml-container ml-root container", [42, 102]),
                composer=("ProseMirror is-empty ExEditor-qq-msg-editor", [42, 103]))


def test_no_state_marker_preserves_original_digest_encoding_and_copies_runtime_values():
    values = shape()
    values["composer"] = ("ProseMirror ExEditor-qq-msg-editor", values["composer"][1])
    expected = hashlib.sha256(json.dumps(list(values.values()), separators=(",", ":")).encode()).hexdigest()
    assert current_chat_structure_digest(**values) == expected
    projected = current_chat_structure_projection(**values)
    values["composer"][1].append(999)
    assert projected[2] == ("ProseMirror ExEditor-qq-msg-editor", (42, 103))


@pytest.mark.parametrize("focused", [
    "ProseMirror-focused ProseMirror is-empty ExEditor-qq-msg-editor",
    "ProseMirror ProseMirror-focused is-empty ExEditor-qq-msg-editor",
    "ProseMirror is-empty ExEditor-qq-msg-editor ProseMirror-focused",
])
def test_only_complete_composer_focus_token_is_transient(focused):
    values = shape()
    baseline = current_chat_structure_digest(**values)
    values["composer"] = (focused, values["composer"][1])
    assert current_chat_structure_digest(**values) == baseline


@pytest.mark.parametrize("role", ["header", "messages"])
@pytest.mark.parametrize("token", ["ProseMirror-focused", "is-empty"])
def test_state_token_on_other_roles_remains_in_identity(role, token):
    values = shape()
    baseline = current_chat_structure_digest(**values)
    class_name, runtime_id = values[role]
    values[role] = (class_name + " " + token, runtime_id)
    assert current_chat_structure_digest(**values) != baseline


@pytest.mark.parametrize("token", ["unknown-state", "ProseMirror-focused-extra", "xProseMirror-focused",
                                   "prosemirror-focused", "ProseMirror-Focused", "is-empty-extra", "xis-empty", "Is-empty"])
def test_no_similar_or_unknown_composer_token_is_ignored(token):
    values = shape()
    baseline = current_chat_structure_digest(**values)
    values["composer"] = (values["composer"][0] + " " + token, values["composer"][1])
    assert current_chat_structure_digest(**values) != baseline


@pytest.mark.parametrize("role", ["header", "messages", "composer"])
def test_every_runtime_id_remains_exact(role):
    values = shape()
    baseline = current_chat_structure_digest(**values)
    values[role] = (values[role][0], [42, 999])
    assert current_chat_structure_digest(**values) != baseline


@pytest.mark.parametrize("class_name", [
    "ProseMirror is-empty",  # Identity class removal is not state normalization.
    "ExEditor-qq-msg-editor is-empty ProseMirror",  # No class sorting.
    "ProseMirror  is-empty ExEditor-qq-msg-editor",  # No general whitespace normalization.
    "ProseMirror  is-empty ExEditor-qq-msg-editor ProseMirror-focused",
])
def test_other_state_order_and_whitespace_remain_exact(class_name):
    values = shape()
    baseline = current_chat_structure_digest(**values)
    values["composer"] = (class_name, values["composer"][1])
    assert current_chat_structure_digest(**values) != baseline


@pytest.mark.parametrize("class_name", [
    "ProseMirror ExEditor-qq-msg-editor",
    "is-empty ProseMirror ExEditor-qq-msg-editor",
    "ProseMirror ExEditor-qq-msg-editor is-empty",
    "ProseMirror-focused is-empty ProseMirror ExEditor-qq-msg-editor",
    "ProseMirror ExEditor-qq-msg-editor is-empty ProseMirror-focused",
    "ProseMirror ProseMirror-focused is-empty ExEditor-qq-msg-editor",
])
def test_only_explicit_composer_state_tokens_can_change_without_instance_change(class_name):
    values = shape()
    baseline = current_chat_structure_digest(**values)
    values["composer"] = (class_name, values["composer"][1])
    assert current_chat_structure_digest(**values) == baseline
