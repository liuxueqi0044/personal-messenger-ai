"""Exact-fingerprint compatibility matrix with fail-closed automation levels."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import ClassVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class VersionGovernanceError(RuntimeError):
    pass


class AutomationLevel(IntEnum):
    L0_OBSERVE_DISABLED = 0
    L1_DRAFT_ONLY = 1
    L2_HUMAN_APPROVED = 2
    L3_WHITELIST_AUTO = 3


class FixtureKind(StrEnum):
    READ_ONLY = "read_only"
    DEDICATED_TEST_SEND = "dedicated_test_send"
    SOAK = "soak"


class CompatibilityStatus(StrEnum):
    UNKNOWN = "unknown"
    OBSERVATION_ONLY = "observation_only"
    TEST_SEND_VALIDATED = "test_send_validated"
    SOAK_VALIDATED = "soak_validated"


class VersionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ClientEnvironmentFingerprint(VersionModel):
    platform: str = Field(pattern=r"^(qq|wechat)$")
    client_version: str = Field(min_length=1, max_length=128)
    client_file_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    windows_version: str = Field(min_length=1, max_length=128)
    dpi_scale: float = Field(gt=0, le=8)
    theme: str = Field(pattern=r"^(light|dark|system|unknown)$")
    window_mode: str = Field(min_length=1, max_length=64)
    window_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter_version: str = Field(min_length=1, max_length=128)

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class FixtureAttestation(VersionModel):
    kind: FixtureKind
    fixture_suite_version: str = Field(min_length=1, max_length=128)
    passed: bool
    sample_count: int = Field(ge=0)
    dedicated_test_account: bool = False
    wrong_recipient_count: int = Field(default=0, ge=0)
    duplicate_send_count: int = Field(default=0, ge=0)
    foreground_contention_count: int = Field(default=0, ge=0)
    account_warning_count: int = Field(default=0, ge=0)
    soak_days: float = Field(default=0, ge=0)
    verified_at: datetime

    @field_validator("verified_at")
    @classmethod
    def aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("verified_at must be timezone-aware")
        return value

    @property
    def has_zero_safety_failures(self) -> bool:
        return not any(
            (
                self.wrong_recipient_count,
                self.duplicate_send_count,
                self.foreground_contention_count,
                self.account_warning_count,
            )
        )


class CompatibilityRecord(VersionModel):
    fingerprint: ClientEnvironmentFingerprint
    status: CompatibilityStatus = CompatibilityStatus.UNKNOWN
    maximum_level: AutomationLevel = AutomationLevel.L0_OBSERVE_DISABLED
    send_quarantined: bool = True
    reason_code: str = "UNKNOWN_FINGERPRINT"
    read_attestation: FixtureAttestation | None = None
    send_attestation: FixtureAttestation | None = None
    soak_attestation: FixtureAttestation | None = None
    updated_at: datetime


class CompatibilityDecision(VersionModel):
    platform: str
    fingerprint_hash: str
    maximum_level: AutomationLevel
    send_quarantined: bool
    reason_code: str
    changed: bool


class _MatrixFile(VersionModel):
    schema_version: int = 1
    active_by_platform: dict[str, str] = Field(default_factory=dict)
    records: dict[str, CompatibilityRecord] = Field(default_factory=dict)


class VersionController:
    READ_FIXTURE_MINIMUM = 500
    SEND_FIXTURE_MINIMUM = 100
    SOAK_DAYS: ClassVar[dict[str, int]] = {"qq": 7, "wechat": 14}

    def __init__(self, matrix_file: str | Path, clock: Callable[[], datetime]) -> None:
        self._path = Path(matrix_file)
        self._clock = clock
        self._matrix = self._load()

    def observe(
        self, fingerprint: ClientEnvironmentFingerprint
    ) -> CompatibilityDecision:
        digest = fingerprint.digest
        previous = self._matrix.active_by_platform.get(fingerprint.platform)
        changed = previous != digest
        if digest not in self._matrix.records:
            self._matrix.records[digest] = CompatibilityRecord(
                fingerprint=fingerprint,
                updated_at=self._clock(),
                reason_code="ENVIRONMENT_CHANGED"
                if previous
                else "UNKNOWN_FINGERPRINT",
            )
        self._matrix.active_by_platform[fingerprint.platform] = digest
        self._persist()
        record = self._matrix.records[digest]
        return self._decision(record, changed=changed)

    def attest(
        self,
        fingerprint: ClientEnvironmentFingerprint,
        attestation: FixtureAttestation,
    ) -> CompatibilityDecision:
        digest = fingerprint.digest
        if self._matrix.active_by_platform.get(fingerprint.platform) != digest:
            raise VersionGovernanceError(
                "attestation fingerprint is not the active environment"
            )
        record = self._matrix.records.get(digest)
        if record is None:
            raise VersionGovernanceError("unknown environment cannot be attested")
        now = self._clock()

        if attestation.kind is FixtureKind.READ_ONLY:
            valid = (
                attestation.passed
                and attestation.sample_count >= self.READ_FIXTURE_MINIMUM
            )
            updated = record.model_copy(
                update={
                    "status": CompatibilityStatus.OBSERVATION_ONLY
                    if valid
                    else CompatibilityStatus.UNKNOWN,
                    "maximum_level": AutomationLevel.L1_DRAFT_ONLY
                    if valid
                    else AutomationLevel.L0_OBSERVE_DISABLED,
                    "send_quarantined": True,
                    "reason_code": "READ_FIXTURES_PASSED"
                    if valid
                    else "READ_FIXTURES_FAILED",
                    "read_attestation": attestation,
                    "send_attestation": None,
                    "soak_attestation": None,
                    "updated_at": now,
                }
            )
        elif attestation.kind is FixtureKind.DEDICATED_TEST_SEND:
            read_ok = (
                record.read_attestation is not None
                and record.read_attestation.passed
                and record.maximum_level >= AutomationLevel.L1_DRAFT_ONLY
            )
            valid = all(
                (
                    read_ok,
                    attestation.passed,
                    attestation.dedicated_test_account,
                    attestation.sample_count >= self.SEND_FIXTURE_MINIMUM,
                    attestation.has_zero_safety_failures,
                )
            )
            updated = record.model_copy(
                update={
                    "status": CompatibilityStatus.TEST_SEND_VALIDATED
                    if valid
                    else record.status,
                    "maximum_level": AutomationLevel.L2_HUMAN_APPROVED
                    if valid
                    else min(record.maximum_level, AutomationLevel.L1_DRAFT_ONLY),
                    "send_quarantined": not valid,
                    "reason_code": "TEST_ACCOUNT_SEND_PASSED"
                    if valid
                    else "TEST_ACCOUNT_SEND_REJECTED",
                    "send_attestation": attestation,
                    "soak_attestation": None,
                    "updated_at": now,
                }
            )
        elif attestation.kind is FixtureKind.SOAK:
            send_ok = (
                record.send_attestation is not None
                and record.maximum_level >= AutomationLevel.L2_HUMAN_APPROVED
            )
            valid = all(
                (
                    send_ok,
                    attestation.passed,
                    attestation.dedicated_test_account,
                    attestation.soak_days >= self.SOAK_DAYS[fingerprint.platform],
                    attestation.has_zero_safety_failures,
                )
            )
            safety_failed = (
                not attestation.passed or not attestation.has_zero_safety_failures
            )
            if valid:
                updates = {
                    "status": CompatibilityStatus.SOAK_VALIDATED,
                    "maximum_level": AutomationLevel.L3_WHITELIST_AUTO,
                    "send_quarantined": False,
                    "reason_code": "SOAK_PASSED",
                }
            elif safety_failed:
                updates = {
                    "status": CompatibilityStatus.OBSERVATION_ONLY,
                    "maximum_level": AutomationLevel.L1_DRAFT_ONLY,
                    "send_quarantined": True,
                    "reason_code": "SOAK_SAFETY_FAILURE",
                }
            else:
                updates = {
                    "status": record.status,
                    "maximum_level": record.maximum_level,
                    "send_quarantined": record.send_quarantined,
                    "reason_code": "SOAK_INCOMPLETE",
                }
            updated = record.model_copy(
                update={
                    **updates,
                    "soak_attestation": attestation,
                    "updated_at": now,
                }
            )
        else:
            raise VersionGovernanceError("unsupported fixture kind")

        self._matrix.records[digest] = updated
        self._persist()
        return self._decision(updated, changed=False)

    def current(self, platform: str) -> CompatibilityDecision:
        digest = self._matrix.active_by_platform.get(platform)
        if digest is None or digest not in self._matrix.records:
            return CompatibilityDecision(
                platform=platform,
                fingerprint_hash="unknown",
                maximum_level=AutomationLevel.L0_OBSERVE_DISABLED,
                send_quarantined=True,
                reason_code="UNKNOWN_FINGERPRINT",
                changed=False,
            )
        return self._decision(self._matrix.records[digest], changed=False)

    def records(self) -> tuple[CompatibilityRecord, ...]:
        return tuple(self._matrix.records.values())

    def _load(self) -> _MatrixFile:
        if not self._path.exists():
            return _MatrixFile()
        try:
            return _MatrixFile.model_validate_json(
                self._path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError):
            raise VersionGovernanceError("compatibility matrix is invalid") from None

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.parent / f".{uuid4().hex}.tmp"
        try:
            temporary.write_text(
                self._matrix.model_dump_json(indent=2), encoding="utf-8"
            )
            os.replace(temporary, self._path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise VersionGovernanceError(
                "compatibility matrix persistence failed"
            ) from None

    @staticmethod
    def _decision(
        record: CompatibilityRecord, *, changed: bool
    ) -> CompatibilityDecision:
        return CompatibilityDecision(
            platform=record.fingerprint.platform,
            fingerprint_hash=record.fingerprint.digest,
            maximum_level=record.maximum_level,
            send_quarantined=record.send_quarantined,
            reason_code=record.reason_code,
            changed=changed,
        )
