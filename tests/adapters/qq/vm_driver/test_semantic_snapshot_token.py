from messenger_ai.adapters.qq.vm_driver.message_cursor import MessageCursorStore


def rows():
    return [
        {"direction":"inbound","text":"first","message_key":"ui-1","conversation_internal_id":"old-window"},
        {"direction":"outbound","text":"reply","message_key":"ui-2","conversation_internal_id":"old-window"},
    ]


def test_fresh_semantic_token_matches_durable_cursor_without_mutating_it():
    cursor = MessageCursorStore(":memory:")
    cursor.ingest_snapshot("conversation",rows())
    before = list(cursor.connection.iterdump())
    fresh = [{**row,"conversation_internal_id":"new-window","observed_at":"later","tree_digest":"new-layout"} for row in rows()]
    assert cursor.semantic_snapshot_token(fresh) == cursor.snapshot_token("conversation")
    assert list(cursor.connection.iterdump()) == before
    cursor.connection.close()


def test_new_message_order_body_direction_or_ui_key_changes_the_token():
    original = rows()
    token = MessageCursorStore.semantic_snapshot_token(original)
    variants = [list(reversed(original)), original+[original[0]]]
    for field,value in (("text","changed"),("direction","outbound"),("message_key","different")):
        variants.append([{**original[0],field:value},original[1]])
    assert all(MessageCursorStore.semantic_snapshot_token(changed) != token for changed in variants)
