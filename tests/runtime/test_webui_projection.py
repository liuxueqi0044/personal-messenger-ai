from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from bs4 import BeautifulSoup
from starlette.testclient import TestClient

from messenger_ai.hub.service import HubService, SQLiteHubStore
from messenger_ai.runtime.state import RuntimeState
from messenger_ai.runtime.webui_projection import RuntimeWebUIProjection
from messenger_ai.webui import LiveHubFacade, create_app


def _runtime(tmp_path):
    state = RuntimeState(tmp_path / "runtime.sqlite")
    for suffix in ("a", "b", "c"):
        state.register(
            account_id="account",
            contact_id=f"contact-{suffix}",
            conversation_id=f"conversation-{suffix}",
            binding_revision=1,
        )
    hub = HubService(SQLiteHubStore(tmp_path / "hub.sqlite"))
    projection = RuntimeWebUIProjection(state=state, hub=hub)
    return state, hub, projection


def test_real_sqlite_projection_lists_only_registered_contacts_and_pages(tmp_path):
    state, hub, projection = _runtime(tmp_path)
    contacts = projection.webui_page("contacts")
    assert {item["contact_id"] for item in contacts["contacts"]} == {
        "contact-a",
        "contact-b",
        "contact-c",
    }
    assert all(item["binding_revision"] == 1 for item in contacts["contacts"])
    for name in ("inbox", "contacts", "rules", "pacing", "adapters", "incidents", "audit", "settings"):
        assert isinstance(projection.webui_page(name), dict)
    assert projection.webui_page("conversation", "conversation-a")["conversation"]["conversation_id"] == "conversation-a"


def test_starlette_all_common_get_pages_are_renderable(tmp_path):
    _state, _hub, projection = _runtime(tmp_path)
    client = TestClient(create_app(LiveHubFacade(projection)))
    for path in ("/inbox", "/contacts", "/contacts/contact-a", "/reviews", "/rules", "/pacing", "/adapters", "/incidents", "/audit", "/settings"):
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)


def test_starlette_get_and_pause_post_use_runtime_revision_cas(tmp_path):
    state, hub, projection = _runtime(tmp_path)
    client = TestClient(create_app(LiveHubFacade(projection)))
    page = client.get("/contacts")
    assert page.status_code == 200
    assert "contact-a" in page.text and "contact-b" in page.text and "contact-c" in page.text
    soup = BeautifulSoup(page.text, "html.parser")
    form = next(form for form in soup.select("form") if form.get("action") == "/actions/pause_contact")
    values = {node.get("name"): node.get("value", "") for node in form.select("input")}
    response = client.post("/actions/pause_contact", data=values)
    assert response.status_code == 200
    assert state.connection.execute(
        "SELECT paused,pause_reason FROM runtime_conversations WHERE contact_id='contact-a'"
    ).fetchone()["paused"] == 1
    values["idempotency_key"] = "stale-pause"
    conflict = client.post("/actions/pause_contact", data=values)
    assert conflict.status_code == 409


def test_uncertain_ack_is_operation_scoped_and_does_not_verify_or_resend(tmp_path):
    state, hub, projection = _runtime(tmp_path)
    conn = hub.store.connection
    draft_id = str(uuid4())
    operation_id = str(uuid4())
    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO drafts(draft_id,conversation_id,contact_id,text,source_message_keys_json,rule_version,text_hash,status,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (draft_id, "conversation-a", "contact-a", "reply", "[]", "rules", "a" * 64, "authorized", now, now),
    )
    conn.execute(
        """INSERT INTO send_operations(operation_id,idempotency_key,draft_id,authorization_id,status,error_code,commit_intent,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (operation_id, "idem-a", draft_id, None, "send_uncertain", "send_uncertain", 1, now, now),
    )
    item = next(item for item in projection.webui_page("contacts")["contacts"] if item["contact_id"] == "contact-a")
    assert item["uncertain_operations"][0]["operation_id"] == operation_id
    result = projection.webui_command(
        "ack_uncertain",
        {
            "idempotency_key": "review-a",
            "entity_id": "contact-a",
                "operation_id": operation_id,
                "expected_revision": item["revision"],
                "expected_contact_revision": item["revision"],
                "expected_operation_revision": item["uncertain_operations"][0]["expected_operation_revision"],
        },
    )
    assert result["accepted"] is True
    row = conn.execute("SELECT status,commit_intent FROM send_operations WHERE operation_id=?", (operation_id,)).fetchone()
    assert (row["status"], row["commit_intent"]) == ("send_uncertain", 1)
    assert state.connection.execute("SELECT COUNT(*) FROM runtime_webui_reviews").fetchone()[0] == 1
    assert projection.webui_command("ack_uncertain", {"idempotency_key": "review-a", "entity_id": "contact-a", "operation_id": operation_id, "expected_revision": item["revision"], "expected_contact_revision": item["revision"], "expected_operation_revision": item["uncertain_operations"][0]["expected_operation_revision"]})["replayed"] is True
