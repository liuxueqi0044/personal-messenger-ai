"""Assess the Q0-Q3 QQ read-only live-driver gates without changing QQ state."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from messenger_ai.adapters.qq.live_driver import (
    CertifiedQQProfile,
    MappingStatus,
    ModalState,
    QQReadOnlyFrontHalf,
    SelectorRole,
    WindowPresentation,
    compile_selector_pack,
    ingest_probe_report,
)

FIXTURE_SUITE_VERSION = "qq-uia-readonly-v2"
READ_ROLES = (
    SelectorRole.MAIN_WINDOW,
    SelectorRole.CONVERSATION_LIST,
    SelectorRole.CONVERSATION_ITEM,
    SelectorRole.MESSAGE_REGION,
)


def _run_probe(max_nodes: int) -> dict[str, Any]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "qq_uia_readonly_probe.py"),
        "--max-nodes",
        str(max_nodes),
        "--include-topology",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
        check=False,
    )
    try:
        value = json.loads(completed.stdout.lstrip("\ufeff"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("QQ probe did not return JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("QQ probe returned a non-object")
    return value


def _candidate_profile(runtime) -> CertifiedQQProfile:
    return CertifiedQQProfile(
        profile_id=f"candidate-{runtime.client_version}",
        allowed_executable_paths=(runtime.executable_path,),
        allowed_executable_signatures=(runtime.executable_signature,),
        allowed_process_signatures=(runtime.process_signature,),
        allowed_window_classes=(runtime.window_class,),
        certified_fingerprint=runtime.fingerprint,
    )


def summarize_candidate(report: dict[str, Any]) -> dict[str, Any]:
    runtime, topology = ingest_probe_report(report, FIXTURE_SUITE_VERSION)
    selector_result = compile_selector_pack(topology, roles=READ_ROLES)
    runtime_suitable = (
        runtime.presentation is WindowPresentation.MAXIMIZED
        and runtime.is_logged_in
        and runtime.modal_state is ModalState.NONE
        and not runtime.is_foreground
    )
    role_status = {
        mapping.role.value: mapping.status.value for mapping in selector_result.mappings
    }
    return {
        "read_only": True,
        "real_send_attempted": False,
        "certification": "candidate_only",
        "runtime": {
            "client_version": runtime.client_version,
            "presentation": runtime.presentation.value,
            "foreground": runtime.is_foreground,
            "logged_in_signal": runtime.is_logged_in,
            "modal_state": runtime.modal_state.value,
            "environment_fingerprint": runtime.fingerprint.digest,
        },
        "q1": {
            "status": selector_result.status.value,
            "roles": role_status,
            "sanitized_node_count": len(topology.nodes),
            "topology_digest": topology.tree_digest,
        },
        "observation_ready": False,
        "candidate_runtime_suitable": runtime_suitable,
        "next_gate": (
            "review_and_certify_profile"
            if runtime_suitable and selector_result.status is MappingStatus.UNIQUE
            else "fix_runtime_or_topology"
        ),
    }


def summarize_certified(
    report: dict[str, Any], profile: CertifiedQQProfile
) -> dict[str, Any]:
    service = QQReadOnlyFrontHalf(
        profile=profile, fixture_suite_version=FIXTURE_SUITE_VERSION
    )
    assessment = service.assess_probe(report)
    return {
        "read_only": True,
        "real_send_attempted": False,
        "certification": "profile_supplied",
        "profile_id": profile.profile_id,
        "status": assessment.status.value,
        "observation_ready": assessment.observation_ready,
        "reasons": assessment.reasons,
        "runtime": {
            "client_version": assessment.runtime.client_version,
            "presentation": assessment.runtime.presentation.value,
            "foreground": assessment.runtime.is_foreground,
            "logged_in_signal": assessment.runtime.is_logged_in,
            "modal_state": assessment.runtime.modal_state.value,
            "environment_fingerprint": assessment.environment.fingerprint.digest,
        },
        "q1": {
            "status": assessment.selector_result.status.value,
            "roles": {
                item.role.value: item.status.value
                for item in assessment.selector_result.mappings
            },
            "selector_pack_version": assessment.selector_pack_version,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Q0-Q3 assessment; never clicks, types, or sends"
    )
    parser.add_argument("--max-nodes", type=int, default=5000)
    parser.add_argument("--profile", type=Path)
    parser.add_argument(
        "--show-candidate-profile",
        action="store_true",
        help="print a candidate profile for manual review; it is not certified",
    )
    args = parser.parse_args()
    if args.max_nodes < 1 or args.max_nodes > 20000:
        parser.error("--max-nodes must be between 1 and 20000")

    report = _run_probe(args.max_nodes)
    runtime, _ = ingest_probe_report(report, FIXTURE_SUITE_VERSION)
    if args.profile is not None:
        profile = CertifiedQQProfile.model_validate_json(
            args.profile.read_text(encoding="utf-8")
        )
        summary = summarize_certified(report, profile)
    else:
        summary = summarize_candidate(report)
        if args.show_candidate_profile:
            if (
                runtime.presentation is not WindowPresentation.MAXIMIZED
                or not runtime.is_logged_in
                or runtime.modal_state is not ModalState.NONE
            ):
                summary["candidate_profile"] = None
                summary["candidate_profile_reason"] = (
                    "profile is emitted only for maximized, logged-in, modal-free QQ"
                )
            else:
                summary["candidate_profile"] = _candidate_profile(runtime).model_dump(
                    mode="json"
                )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary.get("observation_ready") else 2


if __name__ == "__main__":
    raise SystemExit(main())
