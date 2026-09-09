"""Low-coupling M9 ports. They deliberately expose no Adapter or send operation."""

from __future__ import annotations

from typing import Protocol

from .models import InvalidationEvent, PolicyDecision, PolicyRequest


class PolicyPort(Protocol):
    def evaluate_eligibility(self, request: PolicyRequest) -> PolicyDecision: ...

    def revalidate_due(
        self, eligibility: PolicyDecision, request: PolicyRequest
    ) -> PolicyDecision: ...


class AuthorizationInvalidationPort(Protocol):
    def invalidate(self, event: InvalidationEvent) -> int: ...
