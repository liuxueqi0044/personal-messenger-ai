"""Application-facing facade used by the local workbench.

The real Hub can implement the same small protocol.  The web layer deliberately
knows nothing about adapters, model providers, storage paths, or credentials.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4


class HubFacade(Protocol):
    def page(self, name: str, entity_id: str | None = None) -> dict[str, Any]: ...
    def command(self, name: str, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class FakeHubFacade:
    """Deterministic in-memory Hub double for offline and browser-contract tests."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    idempotency: dict[str, dict[str, Any]] = field(default_factory=dict)
    paused: bool = False
    entity_version: int = 1
    contacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    conversations: dict[str, dict[str, Any]] = field(default_factory=dict)
    drafts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.contacts:
            return
        now = datetime.now(UTC).replace(microsecond=0)
        self.contacts["contact-demo"] = {
            "contact_id": "contact-demo",
            "display_name": "示例联系人",
            "platform": "qq",
            "version": 1,
            "risk": "low",
        }
        self.conversations["conversation-demo"] = {
            "conversation_id": "conversation-demo",
            "contact_id": "contact-demo",
            "account_id": "account-local",
            "platform": "qq",
            "version": 1,
            "last_message_at": now.isoformat(),
            "unread": 1,
            "messages": [
                {
                    "direction": "inbound",
                    "text": "今天过得怎么样",
                    "at": now.isoformat(),
                }
            ],
            "plan": {
                "earliest_send_at": (now + timedelta(seconds=30)).isoformat(),
                "status": "waiting",
            },
        }
        self.drafts["draft-demo"] = {
            "draft_id": "draft-demo",
            "conversation_id": "conversation-demo",
            "text": "还不错呀 你呢",
            "status": "created",
            "risk": "low",
            "version": 1,
            "rule_version": "personal-default-v1",
        }

    def _safe_page(self, name: str, entity_id: str | None = None) -> dict[str, Any]:
        if name == "inbox":
            return {
                "conversations": list(self.conversations.values()),
                "paused": self.paused,
            }
        if name == "conversation":
            item = self.conversations.get(entity_id or "")
            if not item:
                raise KeyError(entity_id)
            drafts = [
                d
                for d in self.drafts.values()
                if d["conversation_id"] == item["conversation_id"]
            ]
            return {"conversation": item, "drafts": drafts, "paused": self.paused}
        if name == "reviews":
            return {
                "drafts": [
                    d
                    for d in self.drafts.values()
                    if d["status"] in {"created", "review"}
                ],
                "paused": self.paused,
            }
        if name == "contacts":
            return {"contacts": list(self.contacts.values()), "paused": self.paused}
        if name == "contact":
            item = self.contacts.get(entity_id or "")
            if not item:
                raise KeyError(entity_id)
            return {"contact": item, "paused": self.paused}
        if name == "rules":
            return {
                "rule_version": "personal-default-v1",
                "status": "draft",
                "activation_available": False,
                "paused": self.paused,
            }
        if name == "pacing":
            return {
                "profile": {
                    "quiet_window_seconds": 6,
                    "hard_min_latency_seconds": 8,
                    "long_reply_min_latency_seconds": 30,
                    "segment_max": 3,
                },
                "plans": [c.get("plan") for c in self.conversations.values()],
                "paused": self.paused,
            }
        if name == "adapters":
            return {
                "adapters": [
                    {
                        "platform": "qq",
                        "status": "running-unverified",
                        "background_send": "quarantined",
                    },
                    {
                        "platform": "wechat",
                        "status": "unsupported",
                        "background_send": "quarantined",
                    },
                ],
                "paused": self.paused,
            }
        if name == "incidents":
            return {"incidents": [], "paused": self.paused}
        if name == "audit":
            return {"entries": list(self.calls)[-50:], "paused": self.paused}
        if name == "settings":
            return {
                "automation": "manual-review",
                "retention": "local-only",
                "paused": self.paused,
            }
        raise KeyError(name)

    def page(self, name: str, entity_id: str | None = None) -> dict[str, Any]:
        return self._safe_page(name, entity_id)

    def command(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("idempotency_key", ""))
        if key and key in self.idempotency:
            return {**self.idempotency[key], "replayed": True}
        supplied_version = payload.get("entity_version")
        if (
            supplied_version is not None
            and int(supplied_version) != self.entity_version
        ):
            raise ValueError("stale_version")
        if name == "pause":
            self.paused = True
        elif name == "resume":
            self.paused = False
        elif name == "send_now":
            if payload.get("risk_level", "low") != "l2":
                raise ValueError("manual_l2_required")
            if not payload.get("target_locked", False):
                raise ValueError("target_lock_required")
        elif name not in {"approve_scheduled", "reject", "resubmit", "edit_draft"}:
            raise ValueError("unknown_command")
        self.entity_version += 1
        result = {
            "accepted": True,
            "command": name,
            "command_id": str(uuid4()),
            "version": self.entity_version,
            "replayed": False,
        }
        self.calls.append(
            {
                "action": name,
                "entity_id": payload.get("entity_id", ""),
                "at": datetime.now(UTC).isoformat(),
            }
        )
        if key:
            self.idempotency[key] = result
        return result
