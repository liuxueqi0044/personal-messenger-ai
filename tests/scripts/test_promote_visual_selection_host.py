from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SOURCE = (
    Path(__file__).parents[2]
    / "scripts"
    / "deployment"
    / "host"
    / "promote_visual_selection_host.py"
)
SPEC = importlib.util.spec_from_file_location("promote_visual_selection_host", SOURCE)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _report() -> dict[str, object]:
    return {
        "schema": "pmai-visual-selection-promotion-v2",
        "state": "succeeded",
        "succeeded": True,
        "committed": True,
        "rolled_back": False,
        "accepted_config_sha256": "b" * 64,
        "before_sha256": "a" * 64,
        "after_sha256": "b" * 64,
        "backup_sha256": "a" * 64,
        "binding_count": 3,
        "visual_model": "deepseek-v4-flash-vision-exp",
        "min_confidence": 0.98,
        "semantic_change": "visual_selection_only",
    }


def test_accepts_only_bounded_success_report() -> None:
    assert MODULE._validate(_report())["binding_count"] == 3


@pytest.mark.parametrize(
    "mutation,value",
    [
        ("backup_sha256", "c" * 64),
        ("after_sha256", "a" * 64),
        ("binding_count", 2),
        ("visual_model", "other-model"),
        ("min_confidence", 0.97),
        ("committed", False),
        ("rolled_back", True),
        ("accepted_config_sha256", "c" * 64),
        ("semantic_change", "other"),
    ],
)
def test_rejects_unproven_or_weakened_promotion(mutation: str, value: object) -> None:
    report = _report()
    report[mutation] = value

    with pytest.raises(RuntimeError, match="PROMOTION_REPORT_INVALID"):
        MODULE._validate(report)


def test_rejects_unknown_report_fields() -> None:
    report = _report()
    report["labels"] = ["must-not-export"]
    with pytest.raises(RuntimeError, match="PROMOTION_REPORT_INVALID"):
        MODULE._validate(report)


def _acceptance_report(binding: int, digest: str) -> dict[str, object]:
    attempt = f"00000000-0000-4000-8000-00000000000{binding}"
    return {
        "schema": "pmai-qq-visual-selection-acceptance-host-v1",
        "release_id": "r20260912-23",
        "attempt_id": attempt,
        "binding_id": f"session-contact-{binding}",
        "succeeded": True,
        "acceptance_started": True,
        "builder_exit_code": 0,
        "acceptance_exit_code": 0,
        "guest_result": {"digest": digest},
    }


def test_acceptance_evidence_binds_three_bindings_to_one_digest(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(MODULE, "DIAGNOSTICS", tmp_path)
    digest = "d" * 64
    paths = []
    for binding in (1, 2, 3):
        path = tmp_path / f"acceptance-{binding}.json"
        path.write_text(
            json.dumps(_acceptance_report(binding, digest)), encoding="utf-8"
        )
        paths.append(path)
    visual_host = SimpleNamespace(
        _validate_guest_report=lambda raw, **_kwargs: {
            "succeeded": True,
            "config_sha256": raw["digest"],
        }
    )

    result = MODULE._load_acceptance_evidence(
        paths,
        labels=["1=联系人甲", "2=联系人乙", "3=联系人丙"],
        visual_host=visual_host,
    )

    assert result["accepted_config_sha256"] == digest
    assert result["acceptance_release_id"] == "r20260912-23"
    assert len(result["acceptance_attempt_ids"]) == 3


def test_acceptance_evidence_rejects_mixed_candidate_digests(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(MODULE, "DIAGNOSTICS", tmp_path)
    paths = []
    for binding, digest in ((1, "d" * 64), (2, "e" * 64), (3, "d" * 64)):
        path = tmp_path / f"acceptance-{binding}.json"
        path.write_text(
            json.dumps(_acceptance_report(binding, digest)), encoding="utf-8"
        )
        paths.append(path)
    visual_host = SimpleNamespace(
        _validate_guest_report=lambda raw, **_kwargs: {
            "succeeded": True,
            "config_sha256": raw["digest"],
        }
    )

    with pytest.raises(RuntimeError, match="ACCEPTANCE_CONFIG_DIGEST_MISMATCH"):
        MODULE._load_acceptance_evidence(
            paths,
            labels=["1=联系人甲", "2=联系人乙", "3=联系人丙"],
            visual_host=visual_host,
        )
