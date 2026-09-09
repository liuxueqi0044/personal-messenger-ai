"""Low-cardinality metrics registry with strict names and label values."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from threading import Lock


class MetricError(ValueError):
    pass


class MetricKind(StrEnum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"


@dataclass(frozen=True)
class MetricSpec:
    kind: MetricKind
    labels: Mapping[str, frozenset[str]]


_PLATFORMS = frozenset({"qq", "wechat"})
_REASONS = frozenset(
    {
        "new_inbound",
        "manual_send",
        "rule_changed",
        "capability_changed",
        "pause",
        "expired",
        "stale",
        "rate_limit",
        "send_uncertain",
        "account_warning",
        "captcha",
        "foreground_contention",
        "health_failed",
        "unknown",
    }
)


METRIC_SPECS: dict[str, MetricSpec] = {
    "inbound_events_total": MetricSpec(MetricKind.COUNTER, {"platform": _PLATFORMS}),
    "duplicate_events_total": MetricSpec(MetricKind.COUNTER, {"platform": _PLATFORMS}),
    "drafts_created_total": MetricSpec(MetricKind.COUNTER, {"platform": _PLATFORMS}),
    "policy_blocked_total": MetricSpec(MetricKind.COUNTER, {"reason": _REASONS}),
    "pacing_plans_created_total": MetricSpec(
        MetricKind.COUNTER, {"platform": _PLATFORMS}
    ),
    "pacing_plans_cancelled_total": MetricSpec(
        MetricKind.COUNTER, {"reason": _REASONS}
    ),
    "scheduled_delay_seconds": MetricSpec(
        MetricKind.HISTOGRAM, {"platform": _PLATFORMS}
    ),
    "send_verified_total": MetricSpec(MetricKind.COUNTER, {"platform": _PLATFORMS}),
    "send_uncertain_total": MetricSpec(MetricKind.COUNTER, {"platform": _PLATFORMS}),
    "foreground_contention_total": MetricSpec(
        MetricKind.COUNTER, {"platform": _PLATFORMS}
    ),
    "adapter_quarantined_total": MetricSpec(
        MetricKind.COUNTER,
        {"platform": _PLATFORMS, "reason": _REASONS},
    ),
    "cross_contact_access_denied_total": MetricSpec(
        MetricKind.COUNTER,
        {"component": frozenset({"memory", "policy", "mcp", "webui", "unknown"})},
    ),
    "model_latency_seconds": MetricSpec(
        MetricKind.HISTOGRAM,
        {"provider": frozenset({"openai", "local", "fake", "unknown"})},
    ),
    "model_cost_estimate": MetricSpec(
        MetricKind.COUNTER,
        {"provider": frozenset({"openai", "local", "fake", "unknown"})},
    ),
}


@dataclass(frozen=True)
class MetricPoint:
    name: str
    labels: tuple[tuple[str, str], ...]
    kind: MetricKind
    count: int
    total: float
    minimum: float | None
    maximum: float | None


class MetricsRegistry:
    def __init__(self) -> None:
        self._values: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = {}
        self._lock = Lock()

    def record(self, name: str, value: float = 1, *, labels: Mapping[str, str]) -> None:
        spec = METRIC_SPECS.get(name)
        if spec is None:
            raise MetricError("metric name is not allowlisted")
        if set(labels) != set(spec.labels):
            raise MetricError("metric labels do not match the allowlist")
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise MetricError("metric value must be a finite non-negative number")
        if spec.kind is MetricKind.COUNTER and value == 0:
            return
        normalized: list[tuple[str, str]] = []
        for label_name, allowed_values in spec.labels.items():
            raw = labels[label_name]
            if not isinstance(raw, str) or raw not in allowed_values:
                raise MetricError("metric label value is not allowlisted")
            normalized.append((label_name, raw))
        key = (name, tuple(sorted(normalized)))
        with self._lock:
            self._values.setdefault(key, []).append(float(value))

    def snapshot(self) -> tuple[MetricPoint, ...]:
        with self._lock:
            values = {key: tuple(items) for key, items in self._values.items()}
        points = []
        for (name, labels), observations in sorted(values.items()):
            spec = METRIC_SPECS[name]
            points.append(
                MetricPoint(
                    name=name,
                    labels=labels,
                    kind=spec.kind,
                    count=len(observations),
                    total=sum(observations),
                    minimum=min(observations)
                    if spec.kind is MetricKind.HISTOGRAM
                    else None,
                    maximum=max(observations)
                    if spec.kind is MetricKind.HISTOGRAM
                    else None,
                )
            )
        return tuple(points)
