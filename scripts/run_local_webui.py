"""Run the local workbench; refuse non-loopback hosts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from messenger_ai.adapters.qq.live_driver import CertifiedQQProfile
from messenger_ai.observability import WindowsDPAPISecretStore
from messenger_ai.qq_session_lease_runtime import QQManualSessionScopeReader
from messenger_ai.qq_session_lease_service import (
    create_production_session_lease_service,
)
from messenger_ai.webui import create_app, validate_bind_host

DEFAULT_QQ_PROFILE = PROJECT_ROOT / "profiles" / "qq" / "qq-9.9.26-local-certified.json"
DEFAULT_QQ_APPLICATION = (
    PROJECT_ROOT
    / "state"
    / "qq"
    / "pending-bindings"
    / "f9b741e0-9af3-4d28-a967-816bb74c7e75.json"
)
DEFAULT_QQ_HELPER = (
    PROJECT_ROOT
    / "scripts"
    / "qq_uia_probe_helper"
    / "bin"
    / "Release"
    / "net10.0-windows"
    / "QQ.UiaProbe.exe"
)
DEFAULT_VAULT = PROJECT_ROOT / "state" / "secrets"


def _build_qq_lease_service(args: argparse.Namespace):
    from scripts.qq_live_readonly_assess import _run_probe

    profile = CertifiedQQProfile.model_validate_json(
        args.qq_profile.read_text(encoding="utf-8")
    )
    scope_reader = QQManualSessionScopeReader(
        profile=profile,
        application_path=args.qq_application,
        helper_command=[str(args.qq_helper)],
        read_probe=lambda: _run_probe(5000),
        # The preserved pending application predates the current Q1 selector
        # canonicalization.  Current Q1 must still be uniquely ready and is
        # pinned into every new lease; the application is never rewritten.
        allow_application_selector_rebaseline=True,
    )
    key = WindowsDPAPISecretStore(args.qq_vault).get_or_create_hmac_key(
        "q3.manual-session-lease.hmac"
    )
    return create_production_session_lease_service(
        dpapi_hmac_key=key,
        read_live_scope=scope_reader,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--enable-qq-session-lease", action="store_true")
    parser.add_argument("--qq-profile", type=Path, default=DEFAULT_QQ_PROFILE)
    parser.add_argument("--qq-application", type=Path, default=DEFAULT_QQ_APPLICATION)
    parser.add_argument("--qq-helper", type=Path, default=DEFAULT_QQ_HELPER)
    parser.add_argument("--qq-vault", type=Path, default=DEFAULT_VAULT)
    args = parser.parse_args()
    validate_bind_host(args.host)
    lease_service = None
    if args.enable_qq_session_lease:
        try:
            lease_service = _build_qq_lease_service(args)
        except Exception:  # noqa: BLE001 - startup must not reveal local scope details
            parser.error("QQ session lease setup failed")
    import uvicorn

    uvicorn.run(
        create_app(lease_service=lease_service),
        host=args.host,
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
