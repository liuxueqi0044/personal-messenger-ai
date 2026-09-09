"""Persistent critical-incident isolation with explicit human-only recovery."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IncidentError(RuntimeError):
    pass


class IncidentType(StrEnum):
    SEND_UNCERTAIN = "send_uncertain"
    ACCOUNT_WARNING = "account_warning"
    CAPTCHA = "captcha"
    FOREGROUND_CONTENTION = "foreground_contention"
    REPEATED_TARGET_FAILURE = "repeated_target_failure"


class IncidentStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"


class RuntimeControlPort(Protocol):
    def pause_all(self, reason_code: str) -> None: ...

    def pause_platform(self, platform: str, reason_code: str) -> None: ...

    def quarantine(self, platform: str, reason_code: str) -> None: ...

    def notify(self, incident_id: str, platform: str, reason_code: str) -> None: ...

    def resume_all(self, audit_id: str) -> None: ...

    def resume_platform(self, platform: str, audit_id: str) -> None: ...

    def release_quarantine(self, platform: str, audit_id: str) -> None: ...


class IncidentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IncidentRecord(IncidentModel):
    incident_id: str
    incident_type: IncidentType
    platform: str = Field(pattern=r"^(qq|wechat)$")
    signal_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    status: IncidentStatus
    opened_at: datetime
    resolved_at: datetime | None = None
    resolution_audit_id: str | None = None


class HumanIncidentResolution(IncidentModel):
    audit_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    approver_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=8, max_length=500)
    resume_authorized: bool = False
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved_at must be timezone-aware")
        return value


_SIGNAL_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


class IncidentManager:
    GLOBAL_PAUSE_TYPES = frozenset({IncidentType.ACCOUNT_WARNING, IncidentType.CAPTCHA})

    def __init__(
        self,
        database: str | Path,
        controls: RuntimeControlPort,
        clock: Callable[[], datetime],
    ) -> None:
        self._database = str(database)
        self._controls = controls
        self._clock = clock
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id TEXT PRIMARY KEY,
                    incident_type TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    signal_code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    opened_at TEXT NOT NULL,
                    resolved_at TEXT,
                    resolution_audit_id TEXT,
                    approver_hash TEXT,
                    resolution_reason_hash TEXT
                )
                """
            )

    def raise_incident(
        self,
        incident_type: IncidentType,
        platform: str,
        *,
        signal_code: str,
    ) -> IncidentRecord:
        if platform not in {"qq", "wechat"}:
            raise IncidentError("incident platform is invalid")
        if not _SIGNAL_CODE.fullmatch(signal_code):
            raise IncidentError("incident signal code is invalid")
        now = self._clock()
        incident_id = str(uuid4())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO incidents (incident_id, incident_type, platform, signal_code, status, opened_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    incident_id,
                    incident_type.value,
                    platform,
                    signal_code,
                    IncidentStatus.OPEN.value,
                    now.isoformat(),
                ),
            )
        reason_code = f"INCIDENT_{incident_type.value.upper()}"
        try:
            if incident_type in self.GLOBAL_PAUSE_TYPES:
                self._controls.pause_all(reason_code)
            else:
                self._controls.pause_platform(platform, reason_code)
            self._controls.quarantine(platform, reason_code)
            self._controls.notify(incident_id, platform, reason_code)
        except Exception:  # noqa: BLE001 - external control port must fail closed
            raise IncidentError("incident controls failed closed") from None
        return IncidentRecord(
            incident_id=incident_id,
            incident_type=incident_type,
            platform=platform,
            signal_code=signal_code,
            status=IncidentStatus.OPEN,
            opened_at=now,
        )

    def resolve(
        self,
        incident_id: str,
        resolution: HumanIncidentResolution,
    ) -> IncidentRecord:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            if row is None:
                connection.rollback()
                raise IncidentError("incident does not exist")
            if row["status"] != IncidentStatus.OPEN.value:
                connection.rollback()
                raise IncidentError("incident has already been resolved")
            if resolution.approved_at > now:
                connection.rollback()
                raise IncidentError("resolution approval is from the future")
            import hashlib

            connection.execute(
                """
                UPDATE incidents
                SET status = ?, resolved_at = ?, resolution_audit_id = ?,
                    approver_hash = ?, resolution_reason_hash = ?
                WHERE incident_id = ? AND status = ?
                """,
                (
                    IncidentStatus.RESOLVED.value,
                    now.isoformat(),
                    resolution.audit_id,
                    hashlib.sha256(resolution.approver_id.encode("utf-8")).hexdigest(),
                    hashlib.sha256(resolution.reason.encode("utf-8")).hexdigest(),
                    incident_id,
                    IncidentStatus.OPEN.value,
                ),
            )
            remaining = connection.execute(
                "SELECT COUNT(*) AS count FROM incidents WHERE platform = ? AND status = ?",
                (row["platform"], IncidentStatus.OPEN.value),
            ).fetchone()["count"]
            global_remaining = connection.execute(
                "SELECT COUNT(*) AS count FROM incidents WHERE incident_type IN (?, ?) AND status = ?",
                (
                    IncidentType.ACCOUNT_WARNING.value,
                    IncidentType.CAPTCHA.value,
                    IncidentStatus.OPEN.value,
                ),
            ).fetchone()["count"]

        if resolution.resume_authorized and remaining == 0:
            try:
                self._controls.release_quarantine(row["platform"], resolution.audit_id)
                if row["incident_type"] in {
                    IncidentType.ACCOUNT_WARNING.value,
                    IncidentType.CAPTCHA.value,
                }:
                    if global_remaining == 0:
                        self._controls.resume_all(resolution.audit_id)
                else:
                    self._controls.resume_platform(row["platform"], resolution.audit_id)
            except Exception:  # noqa: BLE001 - external control port must fail closed
                raise IncidentError("manual recovery controls failed closed") from None

        return IncidentRecord(
            incident_id=incident_id,
            incident_type=IncidentType(row["incident_type"]),
            platform=row["platform"],
            signal_code=row["signal_code"],
            status=IncidentStatus.RESOLVED,
            opened_at=datetime.fromisoformat(row["opened_at"]),
            resolved_at=now,
            resolution_audit_id=resolution.audit_id,
        )

    def open_incidents(self) -> tuple[IncidentRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM incidents WHERE status = ? ORDER BY opened_at",
                (IncidentStatus.OPEN.value,),
            ).fetchall()
        return tuple(
            IncidentRecord(
                incident_id=row["incident_id"],
                incident_type=IncidentType(row["incident_type"]),
                platform=row["platform"],
                signal_code=row["signal_code"],
                status=IncidentStatus(row["status"]),
                opened_at=datetime.fromisoformat(row["opened_at"]),
                resolved_at=datetime.fromisoformat(row["resolved_at"])
                if row["resolved_at"]
                else None,
                resolution_audit_id=row["resolution_audit_id"],
            )
            for row in rows
        )
