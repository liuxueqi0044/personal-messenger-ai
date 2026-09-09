from datetime import UTC, datetime, timedelta

from bs4 import BeautifulSoup
from starlette.testclient import TestClient

from messenger_ai.webui import (
    ConfirmResult,
    FakeHubFacade,
    PrepareResult,
    RevokeResult,
    SessionLeaseServiceError,
    StatusResult,
    create_app,
)


class FakeLease:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.challenge = "challenge-opaque"
        self.challenge_csrf = "challenge-csrf-opaque"
        self.lease_id = "lease-opaque"

    def prepare(self, *, session_id: str) -> PrepareResult:
        self.calls.append(("prepare", {"session_id": session_id}))
        return PrepareResult(
            self.challenge,
            self.challenge_csrf,
            datetime.now(UTC) + timedelta(minutes=5),
        )

    def confirm(self, **kwargs: str | bool) -> ConfirmResult:
        self.calls.append(("confirm", {k: str(v) for k, v in kwargs.items()}))
        if kwargs.get("challenge_csrf") != self.challenge_csrf:
            raise SessionLeaseServiceError("csrf_mismatch")
        if kwargs.get("idempotency_key") == "drift":
            raise SessionLeaseServiceError("live_scope_drift")
        if kwargs.get("confirmed") is not True:
            raise SessionLeaseServiceError("explicit_confirmation_required")
        return ConfirmResult(
            self.lease_id, datetime.now(UTC) + timedelta(hours=1), "0123456789ab"
        )

    def status(self, *, session_id: str) -> StatusResult:
        self.calls.append(("status", {"session_id": session_id}))
        return StatusResult("active", self.lease_id)

    def revoke(self, **kwargs: str) -> RevokeResult:
        self.calls.append(("revoke", kwargs))
        return RevokeResult(kwargs["lease_id"], "revoked")


def _client(service: FakeLease | None = None) -> tuple[TestClient, FakeLease]:
    service = service or FakeLease()
    return TestClient(create_app(FakeHubFacade(), lease_service=service)), service


def _fields(client: TestClient) -> dict[str, str]:
    landing = BeautifulSoup(client.get("/qq/session-lease").text, "html.parser")
    csrf = landing.select_one(
        'form[action="/qq/session-lease/prepare"] input[name=csrf_token]'
    )
    assert csrf is not None
    prepared = client.post(
        "/qq/session-lease/prepare", data={"csrf_token": csrf.get("value", "")}
    )
    assert prepared.status_code == 200
    soup = BeautifulSoup(prepared.text, "html.parser")
    return {
        node.get("name"): node.get("value", "")
        for node in soup.select("form input[type=hidden]")
    }


def test_prepare_is_redacted_and_no_store() -> None:
    c, _ = _client()
    landing = c.get("/qq/session-lease")
    assert landing.status_code == 200
    assert "challenge-opaque" not in landing.text
    assert "填写对方 QQ 号" in landing.text
    soup = BeautifulSoup(landing.text, "html.parser")
    csrf = soup.select_one(
        'form[action="/qq/session-lease/prepare"] input[name=csrf_token]'
    )
    assert csrf is not None
    response = c.post(
        "/qq/session-lease/prepare", data={"csrf_token": csrf.get("value", "")}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")
    assert "123456789" not in response.text
    assert "PID" not in response.text and "HWND" not in response.text
    assert "challenge-opaque" in response.text
    assert 'name="qq_number"' not in response.text


def test_prepare_requires_csrf_and_rejects_legacy_sensitive_field() -> None:
    c, service = _client()
    assert c.post("/qq/session-lease/prepare").status_code == 403
    landing = BeautifulSoup(c.get("/qq/session-lease").text, "html.parser")
    csrf = landing.select_one('input[name="csrf_token"]')
    assert csrf is not None
    response = c.post(
        "/qq/session-lease/prepare",
        data={"csrf_token": csrf.get("value", ""), "qq_number": "123456789"},
    )
    assert response.status_code == 400
    assert response.text == "sensitive_field_forbidden"
    assert not any(call[0] == "prepare" for call in service.calls)


def test_confirm_csrf_body_size_and_legacy_sensitive_field_rejected() -> None:
    c, service = _client()
    fields = _fields(c)
    assert (
        c.post(
            "/qq/session-lease/confirm",
            data={**fields, "confirmed": "true"},
            headers={"x": ""},
        ).status_code
        == 200
    )
    confirm_calls = [call for call in service.calls if call[0] == "confirm"]
    assert "qq_number" not in confirm_calls[-1][1]

    c2, _ = _client()
    fields = _fields(c2)
    assert (
        c2.post(
            "/qq/session-lease/confirm",
            data={**fields, "csrf_token": "bad"},
        ).status_code
        == 403
    )
    assert c2.post("/qq/session-lease/confirm", content=b"x" * 4097).status_code == 413

    c3, _ = _client()
    fields = _fields(c3)
    sensitive = c3.post(
        "/qq/session-lease/confirm",
        data={**fields, "qq_number": "123456789", "confirmed": "true"},
    )
    assert sensitive.status_code == 400
    assert sensitive.text == "sensitive_field_forbidden"


def test_confirm_fixed_error_mapping_and_no_generic_hub_command() -> None:
    c, service = _client()
    fields = _fields(c)
    response = c.post(
        "/qq/session-lease/confirm",
        data={**fields, "idempotency_key": "drift", "confirmed": "true"},
    )
    assert response.status_code == 409
    assert response.text.strip() == "live_scope_drift"
    assert not any(call[0] == "command" for call in service.calls)


def test_confirm_rejects_qq_number_in_query() -> None:
    c, _ = _client()
    response = c.post("/qq/session-lease/confirm?qq_number=123456789")

    assert response.status_code == 400
    assert response.text == "sensitive_query_forbidden"


def test_status_and_revoke_are_no_store_and_revoke_is_post_body_only() -> None:
    c, _ = _client()
    assert (
        c.get("/qq/session-lease/status")
        .headers["cache-control"]
        .startswith("no-store")
    )
    fields = _fields(c)
    c.post(
        "/qq/session-lease/confirm",
        data={**fields, "confirmed": "true"},
    )
    session_csrf = fields["csrf_token"]
    response = c.post(
        "/qq/session-lease/revoke",
        data={
            "csrf_token": session_csrf,
            "idempotency_key": "revoke-1",
            "lease_id": "lease-opaque",
        },
    )
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")


def test_unconfigured_service_is_503() -> None:
    c = TestClient(create_app(FakeHubFacade()))
    assert c.get("/qq/session-lease").status_code == 503
    assert c.get("/qq/session-lease/status").status_code == 503
