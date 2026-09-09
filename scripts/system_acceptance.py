"""Run the offline system acceptance bundle and emit machine-readable JSON."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(name: str, args: list[str]) -> dict[str, object]:
    path = ROOT / "scripts" / name
    if not path.exists():
        return {"script": name, "status": "pending", "reason": "script_not_present"}
    completed = subprocess.run(
        [sys.executable, str(path), *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(ROOT / "src"), os.environ.get("PYTHONPATH", "")])
            ),
        },
        check=False,
    )
    stdout = completed.stdout.strip()
    result: dict[str, object] = {
        "script": name,
        "status": "pass" if completed.returncode == 0 else "fail",
        "returncode": completed.returncode,
    }
    if name == "run_llm_evals.py" and stdout:
        try:
            result["report"] = json.loads(stdout)
        except json.JSONDecodeError:
            result["stdout"] = stdout[-1000:]
    else:
        result["stdout"] = stdout[-1000:]
    if completed.stderr.strip():
        result["stderr"] = completed.stderr.strip()[-1000:]
    return result


def main() -> int:
    checks = [
        _run("run_llm_evals.py", ["--json"]),
        _run("policy_matrix.py", []),
        _run("pacing_audit.py", []),
        _run("run_mcp_gateway.py", []),
    ]
    m13 = _run("security_audit.py", [])
    checks.append({"module": "M13", **m13})
    failed = [item for item in checks if item.get("status") == "fail"]
    report = {
        "suite": "offline-system-acceptance",
        "real_send": False,
        "execution_sink": "fake-or-none",
        "overall": "fail" if failed else "pass",
        "checks": checks,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
