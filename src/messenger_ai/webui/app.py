"""Loopback-only local control surface for M11."""

from __future__ import annotations

import html
import secrets
import time
from collections.abc import Iterable
from typing import Any
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from .facade import FakeHubFacade, HubFacade
from .session_lease import SessionLeaseService, SessionLeaseServiceError

SESSION_COOKIE = "mwai_session"
SESSION_TTL_SECONDS = 900
MAX_LEASE_BODY_BYTES = 4096
_sessions: dict[str, dict[str, Any]] = {}


def validate_bind_host(host: str) -> str:
    """Reject wildcard and non-loopback binds; the UI is never a LAN service."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("WebUI only permits loopback binding")
    return host


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _new_session() -> tuple[str, dict[str, Any]]:
    token = secrets.token_urlsafe(32)
    state = {"created": time.monotonic(), "csrf": secrets.token_urlsafe(24)}
    _sessions[token] = state
    return token, state


def _session(request: Request) -> tuple[str, dict[str, Any], bool]:
    token = request.cookies.get(SESSION_COOKIE)
    item = _sessions.get(token or "")
    if not item or time.monotonic() - float(item["created"]) > SESSION_TTL_SECONDS:
        if token:
            _sessions.pop(token, None)
        token, item = _new_session()
        return token, item, True
    return token or "", item, False


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Any) -> Response:
        token, session, fresh = _session(request)
        request.state.session_token, request.state.session = token, session
        response = await call_next(request)
        if fresh:
            response.set_cookie(
                SESSION_COOKIE,
                token,
                max_age=SESSION_TTL_SECONDS,
                httponly=True,
                samesite="lax",
                secure=False,
                path="/",
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Cache-Control", "no-store, no-cache, max-age=0, private"
        )
        response.headers.setdefault("Pragma", "no-cache")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; connect-src 'self'",
        )
        return response


def _form_html(
    csrf: str, action: str, entity_id: str = "", version: int = 1, **extra: str
) -> str:
    hidden = [
        f'<input type="hidden" name="csrf_token" value="{_esc(csrf)}">',
        f'<input type="hidden" name="idempotency_key" value="{_esc(secrets.token_urlsafe(16))}">',
        f'<input type="hidden" name="entity_id" value="{_esc(entity_id)}">',
        f'<input type="hidden" name="entity_version" value="{version}">',
    ]
    hidden += [
        f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">'
        for k, v in extra.items()
    ]
    return f'<form method="post" action="{_esc(action)}">{"".join(hidden)}'


def _layout(request: Request, title: str, body: str) -> HTMLResponse:
    nav = " ".join(
        f'<a href="{path}">{label}</a>'
        for path, label in [
            ("/inbox", "收件箱"),
            ("/reviews", "审核"),
            ("/rules", "规则"),
            ("/pacing", "节奏"),
            ("/adapters", "适配器"),
            ("/incidents", "异常"),
            ("/audit", "审计"),
            ("/settings", "设置"),
            ("/qq/session-lease", "QQ 会话租约"),
        ]
    )
    doc = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_esc(title)}</title><style>body{{font:16px system-ui,sans-serif;max-width:1100px;margin:auto;padding:1rem;line-height:1.5}}nav{{display:flex;gap:.7rem;flex-wrap:wrap;margin-bottom:1rem}}a,button{{padding:.45rem .7rem}}table{{width:100%;border-collapse:collapse}}td,th{{border:1px solid #bbb;padding:.45rem;text-align:left}}@media(max-width:500px){{body{{padding:.65rem}}table{{font-size:.85rem}}.wide{{overflow:auto}}}}</style></head><body><header><h1>{_esc(title)}</h1><nav aria-label="主导航">{nav}</nav></header><main>{body}</main><script>/* countdown is display-only; server decides due state */</script></body></html>"""
    return HTMLResponse(doc)


def _rows(
    items: Iterable[dict[str, Any]], columns: list[tuple[str, str]], links: bool = False
) -> str:
    out = (
        ["<div class=wide><table><thead><tr>"]
        + [f"<th scope=col>{_esc(label)}</th>" for key, label in columns]
        + ["</tr></thead><tbody>"]
    )
    for item in items:
        out.append("<tr>")
        for key, _ in columns:
            value = _esc(item.get(key, ""))
            if links and key == "conversation_id":
                value = f'<a data-entity-id="{value}" href="/conversation/{value}">{value}</a>'
            out.append(f"<td>{value}</td>")
        out.append("</tr>")
    return "".join(out) + "</tbody></table></div>"


async def _get_page(
    request: Request, name: str, entity_id: str | None = None
) -> Response:
    try:
        data = request.app.state.hub.page(name, entity_id)
    except KeyError:
        return PlainTextResponse("not found", status_code=404)
    if name == "inbox":
        status = "已暂停" if data["paused"] else "运行中"
        conversations = _rows(
            data["conversations"],
            [
                ("conversation_id", "会话"),
                ("platform", "平台"),
                ("unread", "未读"),
                ("last_message_at", "最近消息"),
            ],
            True,
        )
        body = f'<p data-state="paused">自动化状态：{status}</p>{conversations}'
    elif name == "conversation":
        c = data["conversation"]
        body = f'<p data-entity-id="{_esc(c["conversation_id"])}">平台 {_esc(c["platform"])} · 联系人 {_esc(c["contact_id"])} · 版本 {_esc(c["version"])}</p>'
        body += _rows(
            c["messages"], [("direction", "方向"), ("text", "消息"), ("at", "时间")]
        )
        body += _rows(
            data["drafts"],
            [
                ("draft_id", "草稿"),
                ("text", "正文"),
                ("status", "状态"),
                ("version", "版本"),
            ],
        )
        body += (
            _form_html(
                request.state.session["csrf"],
                "/actions/approve_scheduled",
                c["conversation_id"],
                c["version"],
            )
            + '<button type="submit">批准按计划发送</button></form>'
        )
        body += (
            _form_html(
                request.state.session["csrf"],
                "/actions/reject",
                c["conversation_id"],
                c["version"],
            )
            + '<button type="submit">拒绝</button></form>'
        )
    elif name == "reviews":
        body = _rows(
            data["drafts"],
            [
                ("draft_id", "草稿"),
                ("conversation_id", "会话"),
                ("text", "正文"),
                ("risk", "风险"),
                ("status", "状态"),
            ],
        )
    elif name == "contacts":
        body = _rows(
            data["contacts"],
            [
                ("contact_id", "联系人"),
                ("display_name", "名称"),
                ("platform", "平台"),
                ("risk", "风险"),
            ],
        )
    elif name == "contact":
        c = data["contact"]
        body = f'<section data-entity-id="{_esc(c["contact_id"])}"><p>显示名 {_esc(c["display_name"])}</p><p>平台 {_esc(c["platform"])}</p><p>风险 {_esc(c["risk"])}</p></section>'
    elif name == "rules":
        body = f'<section data-rule-version="{_esc(data["rule_version"])}"><p>当前规则版本：{_esc(data["rule_version"])}，状态：{_esc(data["status"])}</p><p>第一版工作台不提供规则激活按钮</p></section>'
    elif name == "pacing":
        body = _rows(
            [data["profile"]],
            [
                ("quiet_window_seconds", "静默窗口"),
                ("hard_min_latency_seconds", "最小延迟"),
                ("long_reply_min_latency_seconds", "长文最小延迟"),
                ("segment_max", "最多气泡"),
            ],
        ) + _rows(
            [p for p in data["plans"] if p],
            [("earliest_send_at", "最早发送"), ("status", "状态")],
        )
    elif name == "adapters":
        body = _rows(
            data["adapters"],
            [("platform", "平台"), ("status", "状态"), ("background_send", "后台发送")],
        )
    elif name == "incidents":
        body = (
            _rows(
                data["incidents"],
                [("id", "编号"), ("status", "状态"), ("summary", "摘要")],
            )
            if data["incidents"]
            else "<p>暂无异常</p>"
        )
    elif name == "audit":
        body = (
            _rows(
                data["entries"],
                [("action", "动作"), ("entity_id", "实体"), ("at", "时间")],
            )
            if data["entries"]
            else "<p>暂无审计记录</p>"
        )
    else:
        body = (
            '<section aria-label="settings"><dl>'
            + "".join(f"<dt>{_esc(k)}</dt><dd>{_esc(v)}</dd>" for k, v in data.items())
            + "</dl></section>"
        )
    return _layout(
        request,
        {
            "inbox": "收件箱",
            "conversation": "会话",
            "reviews": "审核队列",
            "contacts": "联系人",
            "contact": "联系人",
            "rules": "规则",
            "pacing": "节奏",
            "adapters": "适配器",
            "incidents": "异常",
            "audit": "审计",
            "settings": "设置",
        }.get(name, name),
        body,
    )


async def _post_action(request: Request) -> Response:
    body = (await request.body()).decode("utf-8", errors="replace")
    values = {
        key: vals[-1] for key, vals in parse_qs(body, keep_blank_values=True).items()
    }
    session = request.state.session
    if values.get("csrf_token") != session["csrf"]:
        return PlainTextResponse("csrf rejected", status_code=403)
    if not values.get("idempotency_key"):
        return PlainTextResponse("idempotency key required", status_code=400)
    try:
        result = request.app.state.hub.command(request.path_params["name"], values)
    except ValueError as exc:
        code = str(exc)
        status = 409 if code == "stale_version" else 400
        return PlainTextResponse("request rejected: " + code, status_code=status)
    return HTMLResponse(
        f'<p data-command-id="{_esc(result["command_id"])}">操作已受理：{_esc(result["command"])} </p><p><a href="/inbox">返回收件箱</a></p>'
    )


def _lease_error(exc: SessionLeaseServiceError) -> Response:
    reason = exc.reason_code
    status = 404 if reason == "lease_not_found" else 409
    if reason == "explicit_confirmation_required":
        status = 400
    return PlainTextResponse(reason, status_code=status)


async def _lease_landing(request: Request) -> Response:
    service = request.app.state.lease_service
    if service is None:
        return PlainTextResponse("lease_service_unavailable", status_code=503)
    body = (
        "<p>这里不需要、也不应该填写对方 QQ 号。</p>"
        "<p>程序只会检查当前已经打开的 QQ 聊天窗口，并为它创建匿名、短时、不可自动发送的会话租约。</p>"
        "<p>点击后只进行只读检测，通常需要几十秒；期间不会点击或输入 QQ。</p>"
        "<form method=post action=/qq/session-lease/prepare>"
        f'<input type=hidden name=csrf_token value="{_esc(request.state.session["csrf"])}">'
        "<button type=submit>检测当前 QQ 会话</button></form>"
    )
    return _layout(request, "QQ 会话租约", body)


async def _lease_prepare(request: Request) -> Response:
    if "qq_number" in request.query_params:
        return PlainTextResponse("sensitive_query_forbidden", status_code=400)
    try:
        declared_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        return PlainTextResponse("content_length_invalid", status_code=400)
    if declared_length < 0 or declared_length > MAX_LEASE_BODY_BYTES:
        return PlainTextResponse("request_too_large", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_LEASE_BODY_BYTES:
        return PlainTextResponse("request_too_large", status_code=413)
    values = {
        k: v[-1]
        for k, v in parse_qs(
            raw.decode("utf-8", errors="replace"), keep_blank_values=True
        ).items()
    }
    if "qq_number" in values:
        values.clear()
        return PlainTextResponse("sensitive_field_forbidden", status_code=400)
    if values.get("csrf_token") != request.state.session["csrf"]:
        values.clear()
        return PlainTextResponse("csrf_rejected", status_code=403)
    values.clear()
    service = request.app.state.lease_service
    if service is None:
        return PlainTextResponse("lease_service_unavailable", status_code=503)
    try:
        result = await run_in_threadpool(
            service.prepare, session_id=request.state.session_token
        )
    except SessionLeaseServiceError as exc:
        return _lease_error(exc)
    except Exception:  # noqa: BLE001 - boundary must not leak adapter errors
        return PlainTextResponse("lease_service_unavailable", status_code=503)
    request.state.session["lease_challenge_id"] = str(result.challenge_id)
    request.state.session["lease_challenge_csrf"] = result.csrf_token
    body = (
        "<p>只读检测完成。请确认当前可见 QQ 会话就是目标联系人。</p>"
        f"<p>申请有效期至 {_esc(result.expires_at.isoformat())}</p>"
        f"<form method=post action=/qq/session-lease/confirm>"
        f'<input type=hidden name=csrf_token value="{_esc(request.state.session["csrf"])}">'
        f'<input type=hidden name=challenge_id value="{_esc(result.challenge_id)}">'
        f'<input type=hidden name=challenge_csrf value="{_esc(result.csrf_token)}">'
        f'<input type=hidden name=idempotency_key value="{_esc(secrets.token_urlsafe(16))}">'
        "<p>无需填写对方 QQ 号；系统只为当前窗口和当前聊天生成匿名短期标识。</p>"
        "<label><input type=checkbox name=confirmed value=true required> 我确认当前可见会话就是目标聊天</label>"
        "<button type=submit>创建短期会话租约</button></form>"
    )
    return _layout(request, "QQ 会话租约申请", body)


async def _lease_confirm(request: Request) -> Response:
    if "qq_number" in request.query_params:
        return PlainTextResponse("sensitive_query_forbidden", status_code=400)
    try:
        declared_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        return PlainTextResponse("content_length_invalid", status_code=400)
    if declared_length < 0 or declared_length > MAX_LEASE_BODY_BYTES:
        return PlainTextResponse("request_too_large", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_LEASE_BODY_BYTES:
        return PlainTextResponse("request_too_large", status_code=413)
    values = {
        k: v[-1]
        for k, v in parse_qs(
            raw.decode("utf-8", errors="replace"), keep_blank_values=True
        ).items()
    }
    session = request.state.session
    if values.get("csrf_token") != session["csrf"]:
        return PlainTextResponse("csrf_rejected", status_code=403)
    if not values.get("idempotency_key"):
        return PlainTextResponse("idempotency_key_required", status_code=400)
    if values.get("challenge_id") != session.get("lease_challenge_id"):
        return PlainTextResponse("challenge_not_found", status_code=409)
    service = request.app.state.lease_service
    if service is None:
        return PlainTextResponse("lease_service_unavailable", status_code=503)
    if "qq_number" in values:
        values.clear()
        return PlainTextResponse("sensitive_field_forbidden", status_code=400)
    try:
        result = await run_in_threadpool(
            service.confirm,
            session_id=request.state.session_token,
            challenge_id=values["challenge_id"],
            challenge_csrf=values.get("challenge_csrf", ""),
            idempotency_key=values["idempotency_key"],
            confirmed=values.get("confirmed") == "true",
        )
    except SessionLeaseServiceError as exc:
        return _lease_error(exc)
    except Exception:  # noqa: BLE001 - boundary must not leak adapter errors
        return PlainTextResponse("lease_service_unavailable", status_code=503)
    finally:
        values.clear()
    session["lease_id"] = str(result.lease_id)
    session.pop("lease_challenge_id", None)
    session.pop("lease_challenge_csrf", None)
    return PlainTextResponse(
        f"lease_created\nlease_id={_esc(result.lease_id)}\nexpires_at={_esc(result.expires_at.isoformat())}\nlease_identity_prefix={_esc(result.lease_identity_prefix)}\nautomatic_eligible={str(result.automatic_eligible).lower()}",
        media_type="text/plain",
    )


async def _lease_status(request: Request) -> Response:
    service = request.app.state.lease_service
    if service is None:
        return PlainTextResponse("lease_service_unavailable", status_code=503)
    try:
        result = await run_in_threadpool(
            service.status, session_id=request.state.session_token
        )
    except SessionLeaseServiceError as exc:
        return _lease_error(exc)
    except Exception:  # noqa: BLE001 - boundary must not leak adapter errors
        return PlainTextResponse("lease_service_unavailable", status_code=503)
    return PlainTextResponse(
        f"state={_esc(result.state)}\nlease_id={_esc(result.lease_id or '')}\nexpires_at={_esc(result.expires_at.isoformat() if result.expires_at else '')}\nautomatic_eligible={str(result.automatic_eligible).lower()}"
    )


async def _lease_revoke(request: Request) -> Response:
    try:
        declared_length = int(request.headers.get("content-length", "0") or 0)
    except ValueError:
        return PlainTextResponse("content_length_invalid", status_code=400)
    if declared_length < 0 or declared_length > MAX_LEASE_BODY_BYTES:
        return PlainTextResponse("request_too_large", status_code=413)
    raw = await request.body()
    if len(raw) > MAX_LEASE_BODY_BYTES:
        return PlainTextResponse("request_too_large", status_code=413)
    values = {
        k: v[-1]
        for k, v in parse_qs(
            raw.decode("utf-8", errors="replace"), keep_blank_values=True
        ).items()
    }
    if values.get("csrf_token") != request.state.session["csrf"]:
        return PlainTextResponse("csrf_rejected", status_code=403)
    if not values.get("idempotency_key") or not values.get("lease_id"):
        return PlainTextResponse("idempotency_key_required", status_code=400)
    service = request.app.state.lease_service
    if service is None:
        return PlainTextResponse("lease_service_unavailable", status_code=503)
    try:
        result = await run_in_threadpool(
            service.revoke,
            session_id=request.state.session_token,
            lease_id=values["lease_id"],
            idempotency_key=values["idempotency_key"],
        )
    except SessionLeaseServiceError as exc:
        return _lease_error(exc)
    except Exception:  # noqa: BLE001 - boundary must not leak adapter errors
        return PlainTextResponse("lease_service_unavailable", status_code=503)
    return PlainTextResponse(
        f"lease_{_esc(result.state)}\nlease_id={_esc(result.lease_id)}"
    )


async def _sse(request: Request) -> Response:
    async def events() -> Any:
        yield 'event: ready\ndata: {"server_time":"now"}\n\n'
        yield ": heartbeat\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
    )


def create_app(
    hub: HubFacade | None = None, lease_service: SessionLeaseService | None = None
) -> Starlette:
    facade = hub or FakeHubFacade()

    async def inbox(r: Request) -> Response:
        return await _get_page(r, "inbox")

    async def conversation(r: Request) -> Response:
        return await _get_page(r, "conversation", r.path_params["conversation_id"])

    async def reviews(r: Request) -> Response:
        return await _get_page(r, "reviews")

    async def contacts(r: Request) -> Response:
        return await _get_page(r, "contacts")

    async def contact(r: Request) -> Response:
        return await _get_page(r, "contact", r.path_params["contact_id"])

    async def named(r: Request) -> Response:
        return await _get_page(r, r.path_params["name"])

    routes = [
        Route("/", lambda r: RedirectResponse("/inbox")),
        Route("/inbox", inbox),
        Route("/conversation/{conversation_id}", conversation),
        Route("/reviews", reviews),
        Route("/contacts", contacts),
        Route("/contacts/{contact_id}", contact),
        Route("/events", _sse),
        Route("/qq/session-lease", _lease_landing, methods=["GET"]),
        Route("/qq/session-lease/prepare", _lease_prepare, methods=["POST"]),
        Route("/qq/session-lease/confirm", _lease_confirm, methods=["POST"]),
        Route("/qq/session-lease/status", _lease_status, methods=["GET"]),
        Route("/qq/session-lease/revoke", _lease_revoke, methods=["POST"]),
        Route("/actions/{name}", _post_action, methods=["POST"]),
        Route("/{name}", named, methods=["GET"], name="named"),
    ]
    app = Starlette(routes=routes, middleware=[Middleware(SecurityMiddleware)])
    app.state.hub = facade
    app.state.lease_service = lease_service
    return app
