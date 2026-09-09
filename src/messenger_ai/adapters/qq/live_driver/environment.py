"""Q0 read-only QQ runtime envelope and window-state sentinel.

This module contains no Windows action APIs.  A platform-specific collector is
injected through ``ReadonlyRuntimePort`` and may only produce a snapshot.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Awaitable
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Q0Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class WindowPresentation(StrEnum):
    MAXIMIZED = "maximized"
    MINIMIZED = "minimized"
    NORMAL = "normal"
    UNKNOWN = "unknown"


class ModalState(StrEnum):
    NONE = "none"
    LOGIN = "login"
    CAPTCHA = "captcha"
    UPDATE = "update"
    ACCOUNT_WARNING = "account_warning"
    UNKNOWN = "unknown"


class RuntimeDecisionReason(StrEnum):
    READY = "ready"
    UNTRUSTED_EXECUTABLE = "untrusted_executable"
    PROCESS_SIGNATURE_DRIFT = "process_signature_drift"
    CLIENT_VERSION_DRIFT = "client_version_drift"
    WINDOW_CLASS_DRIFT = "window_class_drift"
    WINDOW_MINIMIZED = "paused_window_state"
    WINDOW_NOT_MAXIMIZED = "paused_layout_drift"
    WINDOW_STATE_UNKNOWN = "window_state_unknown"
    DPI_DRIFT = "dpi_drift"
    MONITOR_DRIFT = "monitor_drift"
    THEME_DRIFT = "theme_drift"
    TOPOLOGY_DRIFT = "topology_drift"
    MODAL_PRESENT = "modal_present"
    NOT_LOGGED_IN = "not_logged_in"
    QQ_FOREGROUND = "qq_foreground"
    SNAPSHOT_INVALID = "snapshot_invalid"


class WindowBounds(Q0Model):
    left: int
    top: int
    right: int
    bottom: int

    @model_validator(mode="after")
    def positive_area(self) -> WindowBounds:
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("window bounds must have positive area")
        return self


class EnvironmentFingerprint(Q0Model):
    """Stable runtime facts. PID/HWND are intentionally excluded from the digest."""

    platform: Literal["qq"] = "qq"
    executable_path: str = Field(min_length=1)
    executable_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    process_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_version: str = Field(min_length=1)
    windows_version: str = Field(min_length=1)
    dpi_scale: float = Field(gt=0, le=8)
    monitor_id: str = Field(min_length=1)
    monitor_topology_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    theme: str = Field(min_length=1)
    window_class: str = Field(min_length=1)
    presentation: WindowPresentation

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class RuntimeSnapshot(Q0Model):
    """A data-only capture from injected, read-only Windows inspection APIs."""

    platform: Literal["qq"] = "qq"
    process_id: int = Field(gt=0)
    window_handle: int = Field(gt=0)
    executable_path: str = Field(min_length=1)
    executable_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    process_signature: str = Field(pattern=r"^[0-9a-f]{64}$")
    client_version: str = Field(min_length=1)
    windows_version: str = Field(min_length=1)
    dpi_scale: float = Field(gt=0, le=8)
    monitor_id: str = Field(min_length=1)
    monitor_topology_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    theme: str = Field(min_length=1)
    window_class: str = Field(min_length=1)
    presentation: WindowPresentation
    bounds: WindowBounds
    is_foreground: bool = False
    is_occluded: bool = False
    is_logged_in: bool = False
    modal_state: ModalState = ModalState.UNKNOWN

    @property
    def fingerprint(self) -> EnvironmentFingerprint:
        return EnvironmentFingerprint(
            executable_path=self.executable_path,
            executable_signature=self.executable_signature,
            process_signature=self.process_signature,
            client_version=self.client_version,
            windows_version=self.windows_version,
            dpi_scale=self.dpi_scale,
            monitor_id=self.monitor_id,
            monitor_topology_digest=self.monitor_topology_digest,
            theme=self.theme,
            window_class=self.window_class,
            presentation=self.presentation,
        )


class CertifiedQQProfile(Q0Model):
    """An approved exact environment, never a heuristic compatibility range."""

    profile_id: str = Field(min_length=1, max_length=128)
    allowed_executable_paths: tuple[str, ...] = Field(min_length=1)
    allowed_executable_signatures: tuple[str, ...] = Field(min_length=1)
    allowed_process_signatures: tuple[str, ...] = Field(min_length=1)
    allowed_window_classes: tuple[str, ...] = Field(min_length=1)
    certified_fingerprint: EnvironmentFingerprint

    @field_validator(
        "allowed_executable_paths",
        "allowed_executable_signatures",
        "allowed_process_signatures",
        "allowed_window_classes",
    )
    @classmethod
    def no_duplicate_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("profile allowlist cannot contain duplicates")
        return values


class RuntimeDecision(Q0Model):
    observe_allowed: bool
    commit_allowed: bool
    reasons: tuple[RuntimeDecisionReason, ...]
    profile_id: str
    environment_fingerprint: str

    @model_validator(mode="after")
    def fail_closed(self) -> RuntimeDecision:
        if self.commit_allowed and not self.observe_allowed:
            raise ValueError("commit permission requires observation permission")
        if self.commit_allowed and self.reasons != (RuntimeDecisionReason.READY,):
            raise ValueError("commit permission requires an exact ready state")
        return self


class RuntimeEnvelope(Q0Model):
    snapshot: RuntimeSnapshot
    fingerprint: EnvironmentFingerprint
    decision: RuntimeDecision


class ReadonlyRuntimePort(Protocol):
    """Read snapshot only; implementations must not mutate desktop state."""

    def read_snapshot(self) -> RuntimeSnapshot | Awaitable[RuntimeSnapshot]: ...


class QQEnvironmentSentinel:
    def __init__(self, port: ReadonlyRuntimePort, profile: CertifiedQQProfile) -> None:
        self._port = port
        self._profile = profile

    async def inspect(self) -> RuntimeEnvelope:
        snapshot = self._port.read_snapshot()
        if inspect.isawaitable(snapshot):
            snapshot = await snapshot
        if not isinstance(snapshot, RuntimeSnapshot):
            raise TypeError("runtime port must return RuntimeSnapshot")
        return self.assess(snapshot)

    def assess(self, snapshot: RuntimeSnapshot) -> RuntimeEnvelope:
        fingerprint = snapshot.fingerprint
        reasons = self._base_reasons(snapshot, fingerprint)
        if reasons:
            decision = RuntimeDecision(
                observe_allowed=False,
                commit_allowed=False,
                reasons=tuple(reasons),
                profile_id=self._profile.profile_id,
                environment_fingerprint=fingerprint.digest,
            )
        elif snapshot.is_foreground:
            decision = RuntimeDecision(
                observe_allowed=True,
                commit_allowed=False,
                reasons=(RuntimeDecisionReason.QQ_FOREGROUND,),
                profile_id=self._profile.profile_id,
                environment_fingerprint=fingerprint.digest,
            )
        else:
            decision = RuntimeDecision(
                observe_allowed=True,
                commit_allowed=True,
                reasons=(RuntimeDecisionReason.READY,),
                profile_id=self._profile.profile_id,
                environment_fingerprint=fingerprint.digest,
            )
        return RuntimeEnvelope(
            snapshot=snapshot, fingerprint=fingerprint, decision=decision
        )

    def _base_reasons(
        self, snapshot: RuntimeSnapshot, fingerprint: EnvironmentFingerprint
    ) -> list[RuntimeDecisionReason]:
        profile = self._profile
        expected = profile.certified_fingerprint
        reasons: list[RuntimeDecisionReason] = []
        if (
            snapshot.executable_path not in profile.allowed_executable_paths
            or snapshot.executable_signature
            not in profile.allowed_executable_signatures
        ):
            reasons.append(RuntimeDecisionReason.UNTRUSTED_EXECUTABLE)
        if snapshot.process_signature not in profile.allowed_process_signatures:
            reasons.append(RuntimeDecisionReason.PROCESS_SIGNATURE_DRIFT)
        if snapshot.client_version != expected.client_version:
            reasons.append(RuntimeDecisionReason.CLIENT_VERSION_DRIFT)
        if (
            snapshot.window_class not in profile.allowed_window_classes
            or snapshot.window_class != expected.window_class
        ):
            reasons.append(RuntimeDecisionReason.WINDOW_CLASS_DRIFT)
        if snapshot.presentation is WindowPresentation.MINIMIZED:
            reasons.append(RuntimeDecisionReason.WINDOW_MINIMIZED)
        elif snapshot.presentation is WindowPresentation.NORMAL:
            reasons.append(RuntimeDecisionReason.WINDOW_NOT_MAXIMIZED)
        elif snapshot.presentation is WindowPresentation.UNKNOWN:
            reasons.append(RuntimeDecisionReason.WINDOW_STATE_UNKNOWN)
        if snapshot.dpi_scale != expected.dpi_scale:
            reasons.append(RuntimeDecisionReason.DPI_DRIFT)
        if snapshot.monitor_id != expected.monitor_id:
            reasons.append(RuntimeDecisionReason.MONITOR_DRIFT)
        if snapshot.theme != expected.theme:
            reasons.append(RuntimeDecisionReason.THEME_DRIFT)
        if snapshot.monitor_topology_digest != expected.monitor_topology_digest:
            reasons.append(RuntimeDecisionReason.TOPOLOGY_DRIFT)
        if snapshot.modal_state is not ModalState.NONE:
            reasons.append(RuntimeDecisionReason.MODAL_PRESENT)
        if not snapshot.is_logged_in:
            reasons.append(RuntimeDecisionReason.NOT_LOGGED_IN)
        # A versioned fingerprint is exact.  This explicit comparison protects
        # against future fingerprint fields being accidentally ignored above.
        if fingerprint.digest != expected.digest and not reasons:
            reasons.append(RuntimeDecisionReason.SNAPSHOT_INVALID)
        return reasons
