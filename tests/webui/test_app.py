from __future__ import annotations

from bs4 import BeautifulSoup
from starlette.testclient import TestClient

from messenger_ai.webui import FakeHubFacade, LiveHubFacade, create_app, validate_bind_host


def client() -> TestClient:
    return TestClient(create_app(FakeHubFacade()))


def test_all_pages_have_semantic_first_load_and_no_secrets() -> None:
    c = client()
    for path in [
        "/inbox",
        "/conversation/conversation-demo",
        "/reviews",
        "/contacts/contact-demo",
        "/rules",
        "/pacing",
        "/adapters",
        "/incidents",
        "/audit",
        "/settings",
    ]:
        response = c.get(path)
        assert response.status_code == 200
        soup = BeautifulSoup(response.text, "html.parser")
        assert soup.find("main") and (
            soup.find("table") or soup.find("section") or soup.find("p")
        )
        assert "api_key" not in response.text.lower()
        assert "traceback" not in response.text.lower()


def test_csrf_idempotency_and_replay_are_server_side() -> None:
    c = client()
    response = c.get("/conversation/conversation-demo")
    soup = BeautifulSoup(response.text, "html.parser")
    fields = {
        node.get("name"): node.get("value", "")
        for node in soup.select("form:first-of-type input")
    }
    assert (
        c.post(
            "/actions/approve_scheduled", data={**fields, "csrf_token": "bad"}
        ).status_code
        == 403
    )
    first = c.post("/actions/approve_scheduled", data=fields)
    assert first.status_code == 200
    replay = c.post("/actions/approve_scheduled", data=fields)
    assert replay.status_code == 200
    assert "rejected" not in replay.text


def test_stale_version_and_unknown_command_are_rejected() -> None:
    c = client()
    soup = BeautifulSoup(c.get("/conversation/conversation-demo").text, "html.parser")
    fields = {
        node.get("name"): node.get("value", "")
        for node in soup.select("form:first-of-type input")
    }
    fields["entity_version"] = "0"
    assert c.post("/actions/approve_scheduled", data=fields).status_code == 409
    fields["entity_version"] = "1"
    assert c.post("/actions/not-a-domain-command", data=fields).status_code == 400


def test_loopback_only() -> None:
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("localhost") == "localhost"
    for host in ["0.0.0.0", "192.168.1.4", "::"]:
        try:
            validate_bind_host(host)
        except ValueError:
            pass
        else:
            raise AssertionError(host)


def test_production_app_requires_explicit_facade() -> None:
    try:
        create_app()
    except RuntimeError as exc:
        assert "real HubFacade" in str(exc)
    else:
        raise AssertionError("production app silently created a fake facade")


def test_ui_contract_facade_delegates_explicit_runtime_contract() -> None:
    class Runtime:
        def webui_page(self, name, entity_id=None):
            return {"name": name, "entity_id": entity_id}

        def webui_command(self, name, payload):
            return {"command": name, "payload": payload}

    facade = LiveHubFacade(Runtime())
    assert facade.page("contacts")["name"] == "contacts"
    assert facade.command("pause", {"entity_id": "global"})["command"] == "pause"

    try:
        LiveHubFacade(object())
    except TypeError as exc:
        assert "webui_page" in str(exc)
    else:
        raise AssertionError("missing runtime methods were accepted")


def test_ui_contract_can_back_all_common_pages() -> None:
    class Runtime:
        def webui_page(self, name, entity_id=None):
            if name == "inbox":
                return {"paused": False, "revision": 1, "conversations": []}
            if name == "contacts":
                return {"paused": False, "revision": 1, "contacts": []}
            if name == "contact":
                return {"contact": {"contact_id": entity_id, "display_name": "A", "revision": 1}}
            if name == "reviews":
                return {"drafts": []}
            if name == "rules":
                return {"rule_version": "live", "status": "active"}
            if name == "pacing":
                return {"profile": {}, "plans": []}
            if name == "adapters":
                return {"adapters": []}
            if name == "incidents":
                return {"incidents": []}
            if name == "audit":
                return {"entries": []}
            if name == "settings":
                return {"mode": "production"}
            raise KeyError(name)

        def webui_command(self, name, payload):
            return {"command": name, "command_id": "live-1"}

    live = TestClient(create_app(LiveHubFacade(Runtime())))
    for path in ["/inbox", "/contacts", "/contacts/a", "/reviews", "/rules", "/pacing", "/adapters", "/incidents", "/audit", "/settings"]:
        assert live.get(path).status_code == 200


def test_fixture_contact_page_exposes_operational_state_and_controls() -> None:
    response = client().get("/contacts")
    assert response.status_code == 200
    assert "demo-only" in response.text
    assert "绑定有效期" in response.text
    assert "pause_contact" in response.text
