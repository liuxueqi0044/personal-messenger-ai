from __future__ import annotations

import sqlite3

import pytest

from messenger_ai.observability import (
    HumanIncidentResolution,
    IncidentError,
    IncidentManager,
    IncidentType,
)


class FakeControls:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def pause_all(self, reason_code):
        self.calls.append(("pause_all", reason_code))

    def pause_platform(self, platform, reason_code):
        self.calls.append(("pause_platform", platform, reason_code))

    def quarantine(self, platform, reason_code):
        self.calls.append(("quarantine", platform, reason_code))

    def notify(self, incident_id, platform, reason_code):
        self.calls.append(("notify", incident_id, platform, reason_code))

    def resume_all(self, audit_id):
        self.calls.append(("resume_all", audit_id))

    def resume_platform(self, platform, audit_id):
        self.calls.append(("resume_platform", platform, audit_id))

    def release_quarantine(self, platform, audit_id):
        self.calls.append(("release_quarantine", platform, audit_id))


@pytest.mark.parametrize(
    "incident_type",
    [
        IncidentType.SEND_UNCERTAIN,
        IncidentType.ACCOUNT_WARNING,
        IncidentType.CAPTCHA,
        IncidentType.FOREGROUND_CONTENTION,
    ],
)
def test_critical_incidents_pause_quarantine_and_notify(tmp_path, clock, incident_type):
    controls = FakeControls()
    manager = IncidentManager(tmp_path / "incidents.sqlite", controls, clock.now)
    incident = manager.raise_incident(incident_type, "qq", signal_code="FIXTURE_SIGNAL")
    actions = [call[0] for call in controls.calls]
    expected_pause = (
        "pause_all"
        if incident_type in {IncidentType.ACCOUNT_WARNING, IncidentType.CAPTCHA}
        else "pause_platform"
    )
    assert expected_pause in actions
    assert "quarantine" in actions
    assert "notify" in actions
    assert manager.open_incidents()[0].incident_id == incident.incident_id


def test_resolution_requires_explicit_audit_and_resume_flag(tmp_path, clock):
    controls = FakeControls()
    manager = IncidentManager(tmp_path / "incidents.sqlite", controls, clock.now)
    incident = manager.raise_incident(
        IncidentType.SEND_UNCERTAIN, "qq", signal_code="SEND_UNCERTAIN"
    )
    controls.calls.clear()
    audit = HumanIncidentResolution(
        approver_id="local-user",
        reason="I inspected the target conversation and receipt",
        resume_authorized=False,
        approved_at=clock.now(),
    )
    manager.resolve(incident.incident_id, audit)
    assert controls.calls == []
    assert manager.open_incidents() == ()
    with pytest.raises(IncidentError):
        manager.resolve(incident.incident_id, audit)


def test_explicit_human_resolution_can_release_platform(tmp_path, clock):
    controls = FakeControls()
    manager = IncidentManager(tmp_path / "incidents.sqlite", controls, clock.now)
    incident = manager.raise_incident(
        IncidentType.FOREGROUND_CONTENTION, "qq", signal_code="FOREGROUND_CHANGED"
    )
    controls.calls.clear()
    audit = HumanIncidentResolution(
        approver_id="local-user",
        reason="The contention source was inspected and removed",
        resume_authorized=True,
        approved_at=clock.now(),
    )
    manager.resolve(incident.incident_id, audit)
    assert ("release_quarantine", "qq", audit.audit_id) in controls.calls
    assert ("resume_platform", "qq", audit.audit_id) in controls.calls

    with sqlite3.connect(tmp_path / "incidents.sqlite") as connection:
        stored = repr(connection.execute("SELECT * FROM incidents").fetchone())
    assert "local-user" not in stored
    assert "contention source" not in stored


def test_invalid_freeform_signal_is_rejected_before_controls(tmp_path, clock):
    controls = FakeControls()
    manager = IncidentManager(tmp_path / "incidents.sqlite", controls, clock.now)
    with pytest.raises(IncidentError):
        manager.raise_incident(
            IncidentType.CAPTCHA,
            "qq",
            signal_code=r"captcha at C:\Users\alice\shot.png",
        )
    assert controls.calls == []
