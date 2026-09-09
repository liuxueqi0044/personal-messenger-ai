from __future__ import annotations

import pytest

from messenger_ai.observability import METRIC_SPECS, MetricError, MetricsRegistry


def test_every_required_metric_has_a_strict_recording_path():
    registry = MetricsRegistry()
    for name, spec in METRIC_SPECS.items():
        labels = {key: min(values) for key, values in spec.labels.items()}
        registry.record(name, 1, labels=labels)
    assert {point.name for point in registry.snapshot()} == set(METRIC_SPECS)


@pytest.mark.parametrize(
    ("name", "labels"),
    [
        ("unknown_total", {"platform": "qq"}),
        ("inbound_events_total", {"platform": "qq", "nickname": "Alice"}),
        ("inbound_events_total", {"platform": "Alice"}),
        ("policy_blocked_total", {"reason": "alice@example.com"}),
        ("pacing_plans_cancelled_total", {"reason": r"C:\private\shot.png"}),
        ("model_latency_seconds", {"provider": "13800138000"}),
    ],
)
def test_metric_names_and_pii_or_high_cardinality_labels_are_rejected(name, labels):
    with pytest.raises(MetricError):
        MetricsRegistry().record(name, 1, labels=labels)


def test_histogram_snapshot_is_aggregate_only():
    registry = MetricsRegistry()
    registry.record("scheduled_delay_seconds", 8, labels={"platform": "qq"})
    registry.record("scheduled_delay_seconds", 30, labels={"platform": "qq"})
    point = registry.snapshot()[0]
    assert (point.count, point.total, point.minimum, point.maximum) == (2, 38, 8, 30)
