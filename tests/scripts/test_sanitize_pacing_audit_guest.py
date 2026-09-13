"""Maintenance-path guarantees for the guest pacing audit sanitization CLI."""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
SOURCE = REPO_ROOT / "scripts" / "deployment" / "sanitize_pacing_audit_guest.py"
SPEC = importlib.util.spec_from_file_location("sanitize_pacing_audit_guest", SOURCE)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _marker(label: str) -> str:
    return f"SENSITIVE-{label}-0123456789abcdef"


PACING_SCHEMA = """
CREATE TABLE m10_plans (
  pacing_plan_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
  contact_id TEXT NOT NULL, status TEXT NOT NULL,
  earliest_send_at TEXT NOT NULL, expires_at TEXT NOT NULL,
  payload_json TEXT NOT NULL, one_shot_attempt_id TEXT,
  due_emitted_at TEXT, cancel_reason TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE m10_due_outbox (
  outbox_id INTEGER PRIMARY KEY AUTOINCREMENT,
  pacing_plan_id TEXT NOT NULL, segment_index INTEGER NOT NULL,
  payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  operation_id TEXT, claimed_at TEXT, delivered_at TEXT,
  created_at TEXT NOT NULL, one_shot_attempt_id TEXT,
  UNIQUE(pacing_plan_id, segment_index)
);
CREATE TABLE m10_audit (
  audit_id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT,
  action TEXT NOT NULL, reason_code TEXT NOT NULL,
  payload_json TEXT NOT NULL, occurred_at TEXT NOT NULL
);
CREATE TABLE m10_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@pytest.fixture()
def legacy_root(tmp_path: Path) -> dict[str, object]:
    """A pre-migration runtime root with plaintext audit and real business rows."""

    runtime_db = tmp_path / "runtime.sqlite3"
    connection = sqlite3.connect(runtime_db)
    connection.executescript(
        "CREATE TABLE runtime_global_control (singleton INTEGER PRIMARY KEY, "
        "revision INTEGER NOT NULL, paused INTEGER NOT NULL, reason TEXT);"
    )
    connection.execute(
        "INSERT INTO runtime_global_control VALUES(1,4,1,'manual_global_pause')"
    )
    connection.commit()
    connection.close()

    plan_marker = _marker("plan")
    due_marker = _marker("due")
    audit_segment = _marker("audit-segment")
    audit_body = _marker("audit-body")
    audit_bare = _marker("audit-bare")
    audit_corrupt = _marker("audit-corrupt")
    business_plan = json.dumps(
        {"segments": [plan_marker], "text": plan_marker, "status": "waiting"}
    )
    business_due = json.dumps({"body": due_marker, "segment_index": 0})

    pacing_db = tmp_path / "pacing.sqlite3"
    connection = sqlite3.connect(pacing_db)
    connection.executescript(PACING_SCHEMA)
    connection.execute(
        "INSERT INTO m10_plans(pacing_plan_id,conversation_id,contact_id,status,"
        "earliest_send_at,expires_at,payload_json,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "plan-legacy",
            "conv-legacy",
            "contact-legacy",
            "completed",
            "2025-01-01T00:00:08+00:00",
            "2025-01-01T00:05:00+00:00",
            business_plan,
            "2025-01-01T00:00:00+00:00",
            "2025-01-01T00:00:00+00:00",
        ),
    )
    connection.execute(
        "INSERT INTO m10_due_outbox(pacing_plan_id,segment_index,payload_json,"
        "created_at) VALUES(?,?,?,?)",
        ("plan-legacy", 0, business_due, "2025-01-01T00:00:08+00:00"),
    )
    for payload in (
        json.dumps(
            {
                "segments": [audit_segment],
                "body": audit_body,
                "conversation_id": "conv-legacy",
                "status": "waiting",
                "segment_count": 1,
            }
        ),
        json.dumps("bare " + audit_bare),
        "raw not json " + audit_corrupt,
    ):
        connection.execute(
            "INSERT INTO m10_audit(plan_id,action,reason_code,payload_json,"
            "occurred_at) VALUES(?,?,?,?,?)",
            (
                "plan-legacy",
                "plan_created",
                "PACED",
                payload,
                "2025-01-01T00:00:00+00:00",
            ),
        )
    connection.commit()
    connection.close()

    return {
        "root": tmp_path,
        "pacing_db": pacing_db,
        "business_plan": business_plan,
        "business_due": business_due,
        "markers": [
            plan_marker,
            due_marker,
            audit_segment,
            audit_body,
            audit_bare,
            audit_corrupt,
        ],
    }


class _RecordingOwner:
    acquired = 0
    closed = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def acquire(self) -> None:
        type(self).acquired += 1

    def close(self) -> None:
        type(self).closed += 1


class _ContendedOwner:
    closed = 0

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def acquire(self) -> None:
        raise RuntimeError("QQ runtime is already running for this Windows user")

    def close(self) -> None:
        type(self).closed += 1


def _run_cli(root: Path, report: Path, operator: str = "codex-test") -> int:
    argv = [
        "sanitize_pacing_audit_guest.py",
        "--runtime-root",
        str(root),
        "--operator-id",
        operator,
        "--report",
        str(report),
    ]
    original = sys.argv
    sys.argv = argv
    try:
        return MODULE.main()
    finally:
        sys.argv = original


def _audit_payloads(pacing_db: Path) -> list[str]:
    connection = sqlite3.connect(pacing_db)
    try:
        return [
            row[0]
            for row in connection.execute(
                "SELECT payload_json FROM m10_audit ORDER BY audit_id"
            )
        ]
    finally:
        connection.close()


def _business_payloads(pacing_db: Path) -> tuple[str, str]:
    connection = sqlite3.connect(pacing_db)
    try:
        plan = connection.execute(
            "SELECT payload_json FROM m10_plans WHERE pacing_plan_id='plan-legacy'"
        ).fetchone()[0]
        due = connection.execute(
            "SELECT payload_json FROM m10_due_outbox WHERE pacing_plan_id='plan-legacy'"
        ).fetchone()[0]
    finally:
        connection.close()
    return plan, due


def test_cli_refuses_without_exact_global_pause(
    legacy_root: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = legacy_root["root"]
    connection = sqlite3.connect(root / "runtime.sqlite3")
    connection.execute(
        "UPDATE runtime_global_control SET paused=0,reason=NULL WHERE singleton=1"
    )
    connection.commit()
    connection.close()
    _RecordingOwner.acquired = _RecordingOwner.closed = 0
    monkeypatch.setattr(MODULE, "_RuntimeOwner", _RecordingOwner)
    report = tmp_path / "result.json"

    assert _run_cli(root, report) == 2
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload == {
        "schema": MODULE.REPORT_SCHEMA,
        "status": "rejected",
        "error_code": "PACING_AUDIT_GLOBAL_PAUSE_REQUIRED",
    }
    # The migration never ran: audit rows stay plaintext and no marker exists.
    audit_blob = "\u0000".join(_audit_payloads(legacy_root["pacing_db"]))
    assert any(marker in audit_blob for marker in legacy_root["markers"])
    connection = sqlite3.connect(legacy_root["pacing_db"])
    marker_rows = connection.execute("SELECT COUNT(*) FROM m10_metadata").fetchone()[0]
    connection.close()
    assert marker_rows == 0
    assert _RecordingOwner.acquired == 1 and _RecordingOwner.closed == 1


def test_cli_fails_closed_on_missing_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MODULE, "_RuntimeOwner", _RecordingOwner)
    report = tmp_path / "missing-runtime.json"
    assert _run_cli(tmp_path, report, operator="codex-test") == 2
    assert json.loads(report.read_text(encoding="utf-8"))["error_code"] == (
        "PACING_AUDIT_RUNTIME_DB_MISSING"
    )

    root = tmp_path / "paused-no-pacing"
    root.mkdir()
    connection = sqlite3.connect(root / "runtime.sqlite3")
    connection.executescript(
        "CREATE TABLE runtime_global_control (singleton INTEGER PRIMARY KEY, "
        "revision INTEGER NOT NULL, paused INTEGER NOT NULL, reason TEXT);"
        "INSERT INTO runtime_global_control VALUES(1,2,1,'manual_global_pause');"
    )
    connection.commit()
    connection.close()
    report = tmp_path / "missing-pacing.json"
    assert _run_cli(root, report) == 2
    assert json.loads(report.read_text(encoding="utf-8"))["error_code"] == (
        "PACING_AUDIT_PACING_DB_MISSING"
    )


def test_cli_rejects_invalid_operator(tmp_path: Path, legacy_root) -> None:
    report = tmp_path / "bad-operator.json"
    assert _run_cli(legacy_root["root"], report, operator="Operator Upper") == 2
    assert json.loads(report.read_text(encoding="utf-8"))["error_code"] == (
        "PACING_AUDIT_OPERATOR_INVALID"
    )


def test_cli_fails_closed_on_lock_contention(
    legacy_root: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ContendedOwner.closed = 0
    monkeypatch.setattr(MODULE, "_RuntimeOwner", _ContendedOwner)
    report = tmp_path / "contended.json"

    assert _run_cli(legacy_root["root"], report) == 2
    assert json.loads(report.read_text(encoding="utf-8"))["error_code"] == (
        "PACING_AUDIT_OWNER_LOCK_REQUIRED"
    )
    assert _ContendedOwner.closed == 0
    audit_blob = "\u0000".join(_audit_payloads(legacy_root["pacing_db"]))
    assert any(marker in audit_blob for marker in legacy_root["markers"])


def test_cli_migrates_once_and_is_byte_identical_for_business_rows(
    legacy_root: dict[str, object], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MODULE, "_RuntimeOwner", _RecordingOwner)
    report_path = tmp_path / "applied.json"
    before_plan, before_due = _business_payloads(legacy_root["pacing_db"])

    assert _run_cli(legacy_root["root"], report_path) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "applied"
    assert report["operator_id"] == "codex-test"
    assert report["migration_version"] == "redacted-v1"
    assert report["migration_applied"] is True
    assert report["business_byte_identical"] is True
    assert report["plans_before_sha256"] == report["plans_after_sha256"]
    assert report["due_outbox_before_sha256"] == report["due_outbox_after_sha256"]
    assert report["audit_before_sha256"] != report["audit_after_sha256"]
    assert report["counts"] == {
        "audit_rows": 3,
        "audit_rows_rewritten": 3,
        "plans": 1,
        "due_outbox": 1,
    }

    # The report is privacy-minimal: hashes/counts only, no payloads or text.
    raw = report_path.read_text(encoding="utf-8")
    assert "payload_json" not in raw
    for marker in legacy_root["markers"]:
        assert marker not in raw

    # Business rows are untouched byte for byte; audit rows are now plaintext-free.
    after_plan, after_due = _business_payloads(legacy_root["pacing_db"])
    assert after_plan == before_plan == legacy_root["business_plan"]
    assert after_due == before_due == legacy_root["business_due"]
    audit_blob = "\u0000".join(_audit_payloads(legacy_root["pacing_db"]))
    for marker in legacy_root["markers"]:
        assert marker not in audit_blob

    # The pacing scheduler startup migration ran and recorded its marker.
    connection = sqlite3.connect(legacy_root["pacing_db"])
    marker = connection.execute(
        "SELECT value FROM m10_metadata WHERE key=?",
        (MODULE.AUDIT_MIGRATION_KEY,),
    ).fetchone()
    connection.close()
    assert marker is not None and marker[0] == "redacted-v1"

    # Idempotent: a second run rewrites nothing and changes no byte.
    second_path = tmp_path / "noop.json"
    assert _run_cli(legacy_root["root"], second_path) == 0
    second = json.loads(second_path.read_text(encoding="utf-8"))
    assert second["status"] == "noop"
    assert second["migration_applied"] is False
    assert second["idempotent"] is True
    assert second["audit_before_sha256"] == second["audit_after_sha256"]
    assert second["audit_after_sha256"] == report["audit_after_sha256"]
    assert second["plans_after_sha256"] == report["plans_after_sha256"]
    assert second["due_outbox_after_sha256"] == report["due_outbox_after_sha256"]
    assert _business_payloads(legacy_root["pacing_db"]) == (before_plan, before_due)


def test_module_import_loads_no_worker_model_provider_or_send_code() -> None:
    code = (
        "import importlib.util, json, sys\n"
        f"spec = importlib.util.spec_from_file_location('sanitize', r'{SOURCE}')\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "blocked = [name for name in sys.modules if name == 'run_vm_runtime' "
        "or name.startswith(('messenger_ai.adapters', 'messenger_ai.llm', "
        "'messenger_ai.runtime'))]\n"
        "print(json.dumps(sorted(blocked)))\n"
    )
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT)]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=environment,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == []
