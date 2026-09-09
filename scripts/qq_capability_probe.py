"""Read-only QQ installation/process probe; it never starts or automates QQ."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "QQ.exe" in result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only QQ capability probe")
    parser.add_argument(
        "--qq-exe", type=Path, default=Path(r"C:\Program Files\Tencent\QQNT\QQ.exe")
    )
    parser.add_argument("--client-version", default="9.9.26.44343")
    args = parser.parse_args()
    installed, running = args.qq_exe.is_file(), _running()
    status = "unverified"
    if installed and not running:
        status = "installed-but-not-running"
    elif installed and running:
        status = "running-unverified"
    report = {
        "platform": "qq",
        "client_path": str(args.qq_exe),
        "client_version": args.client_version if installed else None,
        "installed": installed,
        "running": running,
        "status": status,
        "quarantined": True,
        "capabilities": {
            "observe_background": "unsupported",
            "resolve_background": "unsupported",
            "compose_background": "unsupported",
            "send_background": "unsupported",
            "verify_background": "unsupported",
        },
        "note": "No UI automation was attempted; a logged-in test account needs a separate guarded POC.",
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
