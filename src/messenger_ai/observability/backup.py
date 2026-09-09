"""Protected SQLite snapshots and fail-closed post-restore recovery policy."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from secrets import token_bytes
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .secrets import SecretNotFoundError, SecretStore


class BackupError(RuntimeError):
    pass


class RecoveryStatePort(Protocol):
    def pause_all(self, reason_code: str) -> None: ...

    def invalidate_all_authorizations(self, reason_code: str) -> int: ...

    def cancel_expired_pacing(self, now: datetime) -> int: ...

    def mark_due_pacing_for_review(self, now: datetime) -> int: ...


class BackupModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BackupManifest(BackupModel):
    schema_version: int = 1
    backup_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    storage_ref: str = Field(pattern=r"^backup\.[0-9a-f]{32}$")
    database_schema_version: str = Field(min_length=1, max_length=128)
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    created_at: datetime
    manifest_hmac: str = Field(pattern=r"^[0-9a-f]{64}$")

    def unsigned_payload(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"manifest_hmac"})


class RestoreApproval(BackupModel):
    audit_id: str = Field(default_factory=lambda: str(uuid4()), min_length=1)
    approver_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=8, max_length=500)
    approved_at: datetime

    @field_validator("approved_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved_at must be timezone-aware")
        return value


class RestoreReport(BackupModel):
    backup_id: str
    audit_id: str
    restored: bool
    system_paused: bool
    authorizations_invalidated: int
    expired_pacing_cancelled: int
    due_pacing_marked_for_review: int
    bulk_dispatch_allowed: bool = False
    completed_at: datetime


class BackupManager:
    def __init__(
        self,
        secret_store: SecretStore,
        clock: Callable[[], datetime],
        *,
        manifest_key_name: str = "backup.manifest.hmac",
    ) -> None:
        self._secret_store = secret_store
        self._clock = clock
        self._manifest_key_name = manifest_key_name

    def create_snapshot(
        self,
        source_database: str | Path,
        manifest_directory: str | Path,
        *,
        database_schema_version: str,
    ) -> Path:
        source_path = Path(source_database).resolve()
        if not source_path.is_file():
            raise BackupError("source database is unavailable")
        snapshot = self._consistent_snapshot(source_path)
        backup_id = uuid4().hex
        storage_ref = f"backup.{backup_id}"
        self._secret_store.set_secret(storage_ref, snapshot)

        unsigned = {
            "schema_version": 1,
            "backup_id": backup_id,
            "storage_ref": storage_ref,
            "database_schema_version": database_schema_version,
            "snapshot_sha256": hashlib.sha256(snapshot).hexdigest(),
            "size_bytes": len(snapshot),
            "created_at": self._clock()
            .astimezone(UTC)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        signature = self._sign(unsigned)
        manifest = BackupManifest(**unsigned, manifest_hmac=signature)
        directory = Path(manifest_directory)
        target = directory / f"backup-{backup_id}.json"
        temporary = directory / f".{backup_id}.tmp"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            temporary.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            temporary.unlink(missing_ok=True)
            self._secret_store.delete_secret(storage_ref)
            raise BackupError("backup manifest persistence failed") from None
        return target

    def restore(
        self,
        manifest_file: str | Path,
        destination_database: str | Path,
        *,
        approval: RestoreApproval,
        recovery: RecoveryStatePort,
    ) -> RestoreReport:
        now = self._clock()
        if approval.approved_at > now:
            raise BackupError("restore approval is from the future")
        manifest = self._load_manifest(manifest_file)
        snapshot = self._secret_store.get_secret(manifest.storage_ref)
        if len(snapshot) != manifest.size_bytes or not hmac.compare_digest(
            hashlib.sha256(snapshot).hexdigest(), manifest.snapshot_sha256
        ):
            raise BackupError("backup snapshot integrity check failed")
        memory = sqlite3.connect(":memory:")
        try:
            memory.deserialize(snapshot)
            if memory.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("backup database integrity check failed")
            try:
                recovery.pause_all("RESTORE_REVIEW_REQUIRED")
            except Exception:  # noqa: BLE001 - external recovery port must fail closed
                raise BackupError("restore pause control failed closed") from None
            self._write_database(memory, Path(destination_database))
        finally:
            memory.close()

        try:
            invalidated = recovery.invalidate_all_authorizations("RESTORE_INVALIDATION")
            cancelled = recovery.cancel_expired_pacing(now)
            review = recovery.mark_due_pacing_for_review(now)
        except Exception:  # noqa: BLE001 - external recovery port must fail closed
            raise BackupError("post-restore safety reconciliation failed") from None
        return RestoreReport(
            backup_id=manifest.backup_id,
            audit_id=approval.audit_id,
            restored=True,
            system_paused=True,
            authorizations_invalidated=invalidated,
            expired_pacing_cancelled=cancelled,
            due_pacing_marked_for_review=review,
            bulk_dispatch_allowed=False,
            completed_at=now,
        )

    def _load_manifest(self, manifest_file: str | Path) -> BackupManifest:
        try:
            manifest = BackupManifest.model_validate_json(
                Path(manifest_file).read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError):
            raise BackupError("backup manifest is invalid") from None
        expected = self._sign(manifest.unsigned_payload())
        if not hmac.compare_digest(expected, manifest.manifest_hmac):
            raise BackupError("backup manifest authentication failed")
        return manifest

    def _sign(self, payload: dict[str, object]) -> str:
        key = self._get_or_create_manifest_key()
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        return hmac.digest(key, canonical, "sha256").hex()

    def _get_or_create_manifest_key(self) -> bytes:
        try:
            key = self._secret_store.get_secret(self._manifest_key_name)
        except SecretNotFoundError:
            key = token_bytes(32)
            self._secret_store.set_secret(self._manifest_key_name, key)
        if len(key) < 32:
            raise BackupError("backup manifest key is invalid")
        return key

    @staticmethod
    def _consistent_snapshot(source_path: Path) -> bytes:
        source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
        memory = sqlite3.connect(":memory:")
        try:
            source.backup(memory)
            if memory.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise BackupError("source database integrity check failed")
            return memory.serialize()
        except sqlite3.Error:
            raise BackupError("database snapshot failed") from None
        finally:
            memory.close()
            source.close()

    @staticmethod
    def _write_database(memory: sqlite3.Connection, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{uuid4().hex}.restore"
        restored = sqlite3.connect(temporary)
        try:
            memory.backup(restored)
            restored.close()
            os.replace(temporary, destination)
        except (OSError, sqlite3.Error):
            restored.close()
            temporary.unlink(missing_ok=True)
            raise BackupError("database restore failed") from None
