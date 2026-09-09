"""Q0-Q3 integration facade for the read-only half of the QQ live driver."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .environment import (
    CertifiedQQProfile,
    QQEnvironmentSentinel,
    RuntimeEnvelope,
    RuntimeSnapshot,
)
from .identity import (
    ConversationBinding,
    HumanBindingConfirmation,
    IdentityBindingRegistry,
    IdentityEvidenceSet,
)
from .observation import (
    BoundObservationScope,
    ExactHwndReadonlyCapturePort,
    InMemoryObservationDedupStore,
    ObservationBatch,
    ObservationDedupPort,
    ReadonlyMessageObserver,
    VisibleConversationSnapshot,
)
from .probe_bridge import ingest_probe_report
from .topology import (
    MappingStatus,
    Q1SelectorPack,
    SelectorCompileResult,
    SelectorRole,
    UiTopologySnapshot,
    compile_selector_pack,
)


class FrontHalfStatus(StrEnum):
    READY = "ready"
    RUNTIME_BLOCKED = "runtime_blocked"
    TOPOLOGY_BLOCKED = "topology_blocked"


class FrontHalfNotReadyError(RuntimeError):
    pass


_READ_ROLES = (
    SelectorRole.MAIN_WINDOW,
    SelectorRole.CONVERSATION_LIST,
    SelectorRole.CONVERSATION_ITEM,
    SelectorRole.MESSAGE_REGION,
)


def _selector_pack_version(pack: Q1SelectorPack) -> str:
    """Version only stable selected-role structure, never runtime UIA ids."""

    payload = {
        "client_version": pack.client_version,
        "environment_fingerprint": pack.environment_fingerprint,
        "fixture_suite_version": pack.fixture_suite_version,
        "topology_digest": pack.topology_digest,
        "selectors": pack.structural_signatures,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "q1:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FrontHalfAssessment:
    profile_id: str
    status: FrontHalfStatus
    runtime: RuntimeSnapshot
    environment: RuntimeEnvelope
    topology: UiTopologySnapshot
    selector_result: SelectorCompileResult
    selector_pack_version: str | None
    reasons: tuple[str, ...]

    @property
    def observation_ready(self) -> bool:
        return (
            self.status is FrontHalfStatus.READY
            and self.environment.decision.observe_allowed
            and self.selector_result.pack is not None
            and self.selector_pack_version is not None
        )


class _UnusedRuntimePort:
    def read_snapshot(self) -> RuntimeSnapshot:
        raise RuntimeError("front-half facade uses explicit probe snapshots")


class QQReadOnlyFrontHalf:
    """Coordinates Q0-Q3 without exposing compose, commit, or send operations."""

    def __init__(
        self,
        *,
        profile: CertifiedQQProfile,
        fixture_suite_version: str,
        registry: IdentityBindingRegistry | None = None,
        dedup_store: ObservationDedupPort | None = None,
        clock: Callable[[], datetime] | None = None,
        minimum_confidence: float = 0.95,
    ) -> None:
        if not fixture_suite_version.strip():
            raise ValueError("fixture_suite_version is required")
        self._profile = profile
        self._fixture_suite_version = fixture_suite_version
        self._registry = registry or IdentityBindingRegistry(
            minimum_confidence=minimum_confidence, clock=clock
        )
        self._dedup_store = dedup_store or InMemoryObservationDedupStore()
        self._clock = clock
        self._minimum_confidence = minimum_confidence
        self._sentinel = QQEnvironmentSentinel(_UnusedRuntimePort(), profile)

    @property
    def registry(self) -> IdentityBindingRegistry:
        return self._registry

    def assess_probe(
        self, report: Mapping[str, Any] | str | bytes
    ) -> FrontHalfAssessment:
        runtime, topology = ingest_probe_report(report, self._fixture_suite_version)
        environment = self._sentinel.assess(runtime)
        selector_result = compile_selector_pack(topology, roles=_READ_ROLES)
        pack_version = (
            _selector_pack_version(selector_result.pack)
            if selector_result.pack is not None
            else None
        )
        reasons = [reason.value for reason in environment.decision.reasons]
        if not environment.decision.observe_allowed:
            status = FrontHalfStatus.RUNTIME_BLOCKED
        elif selector_result.status is not MappingStatus.UNIQUE:
            status = FrontHalfStatus.TOPOLOGY_BLOCKED
            reasons.extend(
                f"{mapping.role.value}:{mapping.status.value}"
                for mapping in selector_result.mappings
                if mapping.status is not MappingStatus.UNIQUE
            )
        else:
            status = FrontHalfStatus.READY
        return FrontHalfAssessment(
            profile_id=self._profile.profile_id,
            status=status,
            runtime=runtime,
            environment=environment,
            topology=topology,
            selector_result=selector_result,
            selector_pack_version=pack_version,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def bind(
        self,
        assessment: FrontHalfAssessment,
        *,
        local_contact_id: str,
        hub_conversation_id: str,
        account_id: str,
        evidence_set: IdentityEvidenceSet,
        confirmation: HumanBindingConfirmation,
    ) -> ConversationBinding:
        self._require_evidence_scope(assessment, evidence_set)
        return self._registry.bind(
            local_contact_id=local_contact_id,
            hub_conversation_id=hub_conversation_id,
            account_id=account_id,
            evidence_set=evidence_set,
            confirmation=confirmation,
        )

    def observe(
        self,
        assessment: FrontHalfAssessment,
        snapshot: VisibleConversationSnapshot,
        *,
        account_id: str,
    ) -> ObservationBatch:
        self._require_evidence_scope(assessment, snapshot.identity_evidence)
        return self._observer(assessment, account_id).observe(snapshot)

    async def capture_and_observe(
        self,
        assessment: FrontHalfAssessment,
        port: ExactHwndReadonlyCapturePort,
        *,
        account_id: str,
    ) -> ObservationBatch:
        self._require_ready(assessment)
        return await self._observer(assessment, account_id).capture_and_observe(port)

    def _observer(
        self, assessment: FrontHalfAssessment, account_id: str
    ) -> ReadonlyMessageObserver:
        self._require_ready(assessment)
        assert assessment.selector_pack_version is not None
        return ReadonlyMessageObserver(
            registry=self._registry,
            scope=BoundObservationScope(
                account_id=account_id,
                process_id=assessment.runtime.process_id,
                window_handle=assessment.runtime.window_handle,
                environment_fingerprint=assessment.environment.fingerprint.digest,
                selector_pack_version=assessment.selector_pack_version,
            ),
            clock=self._clock,
            minimum_confidence=self._minimum_confidence,
            dedup_store=self._dedup_store,
        )

    def _require_ready(self, assessment: FrontHalfAssessment) -> None:
        if assessment.profile_id != self._profile.profile_id:
            raise FrontHalfNotReadyError("assessment belongs to another profile")
        if not assessment.observation_ready:
            raise FrontHalfNotReadyError(
                "Q0-Q1 are not certified for read-only observation"
            )

    def _require_evidence_scope(
        self,
        assessment: FrontHalfAssessment,
        evidence: IdentityEvidenceSet,
    ) -> None:
        self._require_ready(assessment)
        if evidence.window_handle != assessment.runtime.window_handle:
            raise FrontHalfNotReadyError("identity evidence window does not match Q0")
        if (
            evidence.environment_fingerprint
            != assessment.environment.fingerprint.digest
        ):
            raise FrontHalfNotReadyError(
                "identity evidence environment does not match Q0"
            )
        if evidence.selector_pack_version != assessment.selector_pack_version:
            raise FrontHalfNotReadyError("identity evidence does not match Q1")


__all__ = [
    "FrontHalfAssessment",
    "FrontHalfNotReadyError",
    "FrontHalfStatus",
    "QQReadOnlyFrontHalf",
]
