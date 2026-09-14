"""Stage and install a release through the background VirtualBox GuestSession."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(r"C:\Users\xiayu\Documents\Codex\2026-09-09\new-chat")
CONTROL = ROOT / r"outputs\qq-vm\install\control_qq_runtime_host.py"
START_HELPER = ROOT / r"outputs\qq-vm\install\start_qq_runtime_host.py"
GUEST_INSTALLER = ROOT / r"outputs\qq-vm\install\install_frozen_release_guest_v2.py"
DIAGNOSTICS = ROOT / r"outputs\qq-vm\install\diagnostics"
GUEST_MEDIA_ROOT = r"C:\PMAI\data\release-media"
GUEST_STATUS = r"C:\PMAI\data\guest-local-release-install-v2.json"
GUEST_PYTHON = r"C:\PMAI\app\.venv\Scripts\python.exe"
SCHEMA = "pmai-guest-local-release-v1"
MAX_FILE_BYTES = 128 * 1024 * 1024


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def load_release(release_dir: Path) -> tuple[dict[str, Any], list[Path]]:
    root = release_dir.resolve()
    manifest_path = root / "manifest.json"
    if not root.is_dir() or not manifest_path.is_file():
        raise RuntimeError("SOURCE_RELEASE_MISSING")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    release_id = manifest.get("release_id") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA or not isinstance(release_id, str) or not release_id.replace(".", "").replace("-", "").isalnum():
        raise RuntimeError("SOURCE_MANIFEST_INVALID")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries or sum(str(item.get("path", "")).lower().endswith(".whl") for item in entries if isinstance(item, dict)) != 1:
        raise RuntimeError("SOURCE_WHEEL_NOT_UNIQUE")
    files = [manifest_path]
    seen = set()
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise RuntimeError("SOURCE_MANIFEST_FILE_INVALID")
        rel = Path(item["path"])
        if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts) or rel.as_posix() in seen:
            raise RuntimeError("SOURCE_MANIFEST_PATH_INVALID")
        seen.add(rel.as_posix())
        source = root / rel
        if source.is_symlink() or not source.is_file() or source.stat().st_size != int(item.get("length", -1)) or source.stat().st_size > MAX_FILE_BYTES or sha256(source) != str(item.get("sha256", "")).upper():
            raise RuntimeError("SOURCE_RELEASE_HASH_INVALID")
        files.append(source)
    return manifest, files


def control_module():
    spec = importlib.util.spec_from_file_location("pmai_release_control", CONTROL)
    if spec is None or spec.loader is None:
        raise RuntimeError("CONTROL_HELPER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def start_module():
    spec = importlib.util.spec_from_file_location("pmai_release_start", START_HELPER)
    if spec is None or spec.loader is None:
        raise RuntimeError("START_HELPER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def assert_runtime_stopped(runtime: object, start: object, guest: object) -> None:
    if not isinstance(runtime, dict):
        raise RuntimeError("RUNTIME_STOP_STATE_UNPROVEN")
    if runtime.get("runtime_process_alive") is not False:
        raise RuntimeError("RUNTIME_STOP_STATE_UNPROVEN")
    runtime_pid = runtime.get("runtime_process_id")
    if (
        not isinstance(runtime_pid, int)
        or isinstance(runtime_pid, bool)
        or runtime_pid <= 0
    ):
        raise RuntimeError("RUNTIME_PID_UNAVAILABLE")
    if start._guest_pid_alive(guest, runtime_pid):
        raise RuntimeError("RUNTIME_NOT_STOPPED")


def ensure_guest_parent(guest: object, path: str) -> None:
    normalized = path.replace("/", "\\").rstrip("\\")
    drive, tail = (normalized[:2], normalized[2:]) if len(normalized) >= 2 and normalized[1] == ":" else ("", normalized)
    current = drive + "\\" if drive else ""
    for part in tail.strip("\\").split("\\"):
        if not part:
            continue
        current = current.rstrip("\\") + "\\" + part
        try:
            guest.DirectoryCreate(current, 0, [])
        except Exception:
            pass


def run_guest(guest: object, installer: str, staging: str, release_id: str) -> tuple[int, int]:
    process = guest.ProcessCreate(GUEST_PYTHON, [GUEST_PYTHON, installer, "--staging", staging, "--release-id", release_id], r"C:\PMAI\data", [], [], 300_000)
    if process.WaitForArray([1], 30_000) != 1:
        raise RuntimeError("INSTALL_PROCESS_DID_NOT_START")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        result = process.WaitForArray([2, 4], 500)
        if result == 2:
            return int(process.Status), int(process.ExitCode)
        if result == 4:
            raise RuntimeError("INSTALL_PROCESS_WAIT_ERROR")
    raise RuntimeError("INSTALL_PROCESS_TIMEOUT")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-dir", type=Path, required=True)
    args = parser.parse_args()
    report: dict[str, Any] = {"schema": "pmai-guest-release-host-install-v2", "succeeded": False}
    vbox = host = guest = None
    clear = bytearray()
    stage = ""
    try:
        release_root = args.release_dir.resolve()
        manifest, files = load_release(release_root)
        release_id = str(manifest["release_id"])
        report["release_id"] = release_id
        control = control_module()
        vbox, host, guest, clear, _password = control._open_guest()
        runtime = control._runtime_status(guest, release_id + "-preinstall")
        assert_runtime_stopped(runtime, start_module(), guest)
        stage = GUEST_MEDIA_ROOT + "\\" + release_id + "-" + uuid.uuid4().hex
        try:
            guest.DirectoryCreate(GUEST_MEDIA_ROOT, 0, [])
        except Exception:
            pass
        guest.DirectoryCreate(stage, 0, [])
        if not GUEST_INSTALLER.is_file():
            raise RuntimeError("GUEST_INSTALLER_MISSING")
        copied = [("manifest.json", release_root / "manifest.json")]
        copied.extend((str(path.relative_to(release_root)).replace("/", "\\"), path) for path in files[1:])
        copied.append(("install_frozen_release_guest_v2.py", GUEST_INSTALLER))
        for relative, source in copied:
            target = stage + "\\" + relative
            ensure_guest_parent(guest, target.rsplit("\\", 1)[0])
            control._wait_progress(guest.FileCopyToGuest(str(source), target, []), 30_000)
            probe = DIAGNOSTICS / ("." + uuid.uuid4().hex + ".roundtrip")
            try:
                control._wait_progress(guest.FileCopyFromGuest(target, str(probe), []), 30_000)
                if sha256(source) != sha256(probe):
                    raise RuntimeError("RELEASE_ROUNDTRIP_HASH_MISMATCH")
            finally:
                if probe.exists():
                    probe.unlink()
        status_code, exit_code = run_guest(guest, stage + r"\install_frozen_release_guest_v2.py", stage, release_id)
        status_path = DIAGNOSTICS / ("guest-local-release-install-" + release_id + ".json")
        status = control._copy_from_guest(guest, GUEST_STATUS, status_path)
        target_manifest_path = DIAGNOSTICS / ("target-manifest-" + release_id + ".json")
        target_manifest = control._copy_from_guest(guest, f"C:\\PMAI\\app\\releases\\{release_id}\\manifest.json", target_manifest_path)
        if exit_code != 0 or status.get("succeeded") is not True or status.get("release_id") != release_id or target_manifest != manifest:
            raise RuntimeError("INSTALL_RESULT_INVALID")
        report.update({"stage": stage, "process_status": status_code, "installer_exit_code": exit_code, "install_status": {k: status.get(k) for k in ("state", "succeeded", "release_id", "error_code")}, "target_manifest_sha256": sha256(target_manifest_path), "succeeded": True})
        return 0
    except Exception as exc:
        report["error_code"] = str(exc)[:128]
        return 2
    finally:
        DIAGNOSTICS.mkdir(parents=True, exist_ok=True)
        (DIAGNOSTICS / "stage-install-guest-release-v2.json").write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        for index in range(len(clear)):
            clear[index] = 0
        if guest is not None:
            try: guest.Close()
            except Exception: pass
        if host is not None:
            try: host.UnlockMachine()
            except Exception: pass


if __name__ == "__main__":
    raise SystemExit(main())
