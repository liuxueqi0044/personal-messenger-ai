from __future__ import annotations

from bs4 import BeautifulSoup
from starlette.testclient import TestClient

from messenger_ai.webui import FakeHubFacade, create_app, validate_bind_host


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
