import pytest
from messenger_ai.adapters.qq.vm_driver.message_decoder import MessageDecodeError, decode_message_region

class Node:
    def __init__(self, cls="", name="", control="Group", children=()):
        self.ClassName, self.Name, self.ControlTypeName = cls, name, control
        self.children = list(children)
    def GetChildren(self): return list(self.children)
    def GetTextPattern(self): return None

def content(direction, text):
    leaf = Node(name=text, control="Text")
    body = Node(cls=f"msg-content-container {direction} mix-message__container", children=(leaf,))
    container = Node(cls="message-container message-container--mix-or-markdown", children=(body,))
    return Node(cls="message", children=(container,))

def test_real_shape_decodes_four_inbound_one_outbound_and_skips_gray_tip():
    rows = [content("container--others", f"in-{i}") for i in range(4)]
    rows += [Node(cls="message", children=(Node(cls="gray-tip-message no-copy", children=(Node(name="system", control="Text"),)),)),
             content("container--self", "out")]
    decoded = decode_message_region(Node(cls="q-scroll-view ml-root container", children=rows))
    assert [item.direction.value for item in decoded] == ["inbound"] * 4 + ["outbound"]

def test_direction_conflict_and_media_only_are_explicit_gaps():
    with pytest.raises(MessageDecodeError, match="direction"):
        decode_message_region(Node(cls="ml-root", children=(content("container--self container--others", "x"),)))
    media = Node(cls="message", children=(Node(cls="message-container", children=(Node(cls="msg-content-container container--others", children=(Node(control="Image"),)),)),))
    with pytest.raises(MessageDecodeError, match="unsupported"):
        decode_message_region(Node(cls="ml-root", children=(media,)))

def test_repeated_text_is_preserved_for_sequence_cursor_alignment():
    root = Node(cls="ml-root", children=(content("container--others", "same"), content("container--others", "same")))
    decoded = decode_message_region(root)
    assert [item.text for item in decoded] == ["same", "same"]
    assert decoded[0].message_key == decoded[1].message_key


def test_python_uia_list_wrappers_decode_in_observed_order():
    messages = (content("container--others", "first"),
                content("container--self", "second"))
    wrappers = tuple(Node(cls="ml-item", children=(message,)) for message in messages)
    root = Node(cls="q-scroll-view ml-root container",
                children=(Node(cls="ml-list list", children=wrappers),))
    decoded = decode_message_region(root)
    assert [(item.direction.value, item.text) for item in decoded] == [
        ("inbound", "first"), ("outbound", "second")]


def test_python_uia_empty_list_is_empty_snapshot():
    root = Node(cls="ml-root", children=(Node(cls="ml-list list"),))
    assert decode_message_region(root) == ()


def test_python_uia_unknown_list_wrapper_fails_closed():
    root = Node(cls="ml-root", children=(Node(cls="ml-list list", children=(
        Node(cls="virtual-placeholder"),)),))
    with pytest.raises(MessageDecodeError, match="wrapper"):
        decode_message_region(root)


@pytest.mark.parametrize("row", [
    Node(cls="message", children=(Node(cls="message-container"),)),
    Node(cls="message", children=(Node(cls="message-container", children=(Node(cls="msg-content-container container--others", children=(Node(control="Text"),)),)),)),
    Node(cls="message", children=(Node(cls="message", children=(content("container--others", "x"),)),)),
])
def test_unknown_empty_and_nested_rows_fail_closed(row):
    with pytest.raises(MessageDecodeError):
        decode_message_region(Node(cls="ml-root", children=(row,)))


def test_text_plus_media_is_not_treated_as_complete_text():
    body = Node(cls="msg-content-container container--others", children=(Node(name="caption", control="Text"), Node(control="Image")))
    row = Node(cls="message", children=(Node(cls="message-container", children=(body,)),))
    with pytest.raises(MessageDecodeError, match="unsupported"):
        decode_message_region(Node(cls="ml-root", children=(row,)))
