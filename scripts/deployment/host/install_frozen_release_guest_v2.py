"""Install a validated release from a guest-local staging directory.

This script is intentionally non-interactive. It emits only a redacted status
file and never prints package output, credentials, or desktop/UI state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

SCHEMA = "pmai-guest-local-release-v1"
INSTALL_SCHEMA = "pmai-guest-local-release-install-v2"
MEDIA_ROOT = Path(r"C:\PMAI\data\release-media")
TARGET_ROOT = Path(r"C:\PMAI\app\releases")
STATUS = Path(r"C:\PMAI\data\guest-local-release-install-v2.json")
PYTHON = Path(r"C:\PMAI\app\.venv\Scripts\python.exe")
MAX_FILE_BYTES = 128 * 1024 * 1024
def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def safe_relative(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RuntimeError("MANIFEST_PATH_INVALID")
    path = Path(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise RuntimeError("MANIFEST_PATH_INVALID")
    return path


def inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def load_manifest(staging: Path, expected_release_id: str) -> tuple[dict[str, Any], list[tuple[Path, int, str]]]:
    manifest_path = staging / "manifest.json"
    if staging.is_symlink() or not inside(MEDIA_ROOT, staging) or not manifest_path.is_file():
        raise RuntimeError("MANIFEST_MISSING")
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError("MANIFEST_INVALID") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA or value.get("release_id") != expected_release_id:
        raise RuntimeError("MANIFEST_IDENTITY_INVALID")
    entries = value.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("MANIFEST_FILES_INVALID")
    parsed: list[tuple[Path, int, str]] = []
    seen: set[str] = set()
    for item in entries:
        if not isinstance(item, dict):
            raise RuntimeError("MANIFEST_FILE_INVALID")
        rel = safe_relative(item.get("path"))
        key = rel.as_posix()
        digest = item.get("sha256")
        length = item.get("length")
        if key in seen or not isinstance(length, int) or length < 0:
            raise RuntimeError("MANIFEST_FILE_INVALID")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in digest):
            raise RuntimeError("MANIFEST_FILE_INVALID")
        seen.add(key)
        parsed.append((rel, length, digest.upper()))
    if sum(rel.suffix.lower() == ".whl" for rel, _length, _digest in parsed) != 1:
        raise RuntimeError("WHEEL_NOT_UNIQUE")
    return value, parsed


def verify_files(root: Path, entries: list[tuple[Path, int, str]]) -> None:
    for rel, length, expected in entries:
        path = root / rel
        if not inside(root, path) or not path.is_file() or path.is_symlink():
            raise RuntimeError("RELEASE_FILE_MISSING")
        if path.stat().st_size != length or length > MAX_FILE_BYTES or sha256(path) != expected:
            raise RuntimeError("RELEASE_FILE_HASH_MISMATCH")


def save_status(value: dict[str, Any]) -> None:
    STATUS.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".guest-release-v2-", suffix=".json", dir=str(STATUS.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, STATUS)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def guest_identity() -> None:
    if os.name != "nt" or os.environ.get("COMPUTERNAME", "").upper() != "PMAI-QQVM":
        raise RuntimeError("GUEST_IDENTITY_INVALID")
    if os.environ.get("USERNAME", "").lower() != "qqbot":
        raise RuntimeError("GUEST_USER_INVALID")
    import ctypes
    if bool(ctypes.windll.shell32.IsUserAnAdmin()):
        raise RuntimeError("GUEST_ADMIN_FORBIDDEN")
    probe = ("$cs=Get-CimInstance Win32_ComputerSystem;"
             "if($cs.Name -cne 'PMAI-QQVM' -or $cs.Model -notmatch 'VirtualBox' -or "
             "$cs.Manufacturer -notmatch '(Oracle|innotek)'){exit 40}")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", probe],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode != 0:
        raise RuntimeError("GUEST_VIRTUALBOX_INVALID")


def verify_wheel_modules(wheel: Path, installed_root: Path) -> dict[str, str]:
    expected: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.startswith("messenger_ai/") and name.endswith(".py")
        ]
        if not names or len(names) != len(set(names)):
            raise RuntimeError("WHEEL_MODULE_SET_INVALID")
        for name in names:
            relative = safe_relative(name)
            installed = installed_root / relative
            if not inside(installed_root, installed):
                raise RuntimeError("WHEEL_MODULE_PATH_INVALID")
            expected[name] = hashlib.sha256(archive.read(name)).hexdigest().upper()
    for name, digest in expected.items():
        installed = installed_root / Path(name)
        if not installed.is_file() or sha256(installed) != digest:
            raise RuntimeError("INSTALLED_MODULE_HASH_MISMATCH")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    report: dict[str, Any] = {"schema": INSTALL_SCHEMA, "release_id": args.release_id, "state": "running", "succeeded": False}
    incoming: Path | None = None
    try:
        guest_identity()
        if not args.release_id or not args.release_id.replace(".", "").replace("-", "").isalnum():
            raise RuntimeError("RELEASE_ID_INVALID")
        staging = args.staging.resolve()
        manifest, entries = load_manifest(staging, args.release_id)
        verify_files(staging, entries)
        report["manifest_sha256"] = sha256(staging / "manifest.json")
        target = TARGET_ROOT / args.release_id
        if target.exists():
            raise RuntimeError("TARGET_ALREADY_EXISTS")
        TARGET_ROOT.mkdir(parents=True, exist_ok=True)
        incoming = TARGET_ROOT / (f".{args.release_id}.incoming-{uuid.uuid4().hex}")
        incoming.mkdir()
        shutil.copy2(staging / "manifest.json", incoming / "manifest.json")
        if sha256(incoming / "manifest.json") != report["manifest_sha256"]:
            raise RuntimeError("MANIFEST_COPY_HASH_MISMATCH")
        for rel, _length, _digest in entries:
            destination = incoming / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(staging / rel, destination)
        verify_files(incoming, entries)
        wheel = next(incoming / rel for rel, _length, _digest in entries if rel.suffix.lower() == ".whl")
        if Path(sys.executable).resolve() != PYTHON.resolve():
            raise RuntimeError("PYTHON_EXECUTABLE_MISMATCH")
        completed = subprocess.run([str(PYTHON), "-m", "pip", "install", "--no-index", "--no-deps", "--force-reinstall", str(wheel)],
                                   check=False, capture_output=True)
        report.update({"pip_returncode": completed.returncode, "pip_stdout_bytes": len(completed.stdout or b""), "pip_stderr_bytes": len(completed.stderr or b"")})
        if completed.returncode != 0:
            raise RuntimeError("PIP_INSTALL_FAILED")
        report["module_hashes"] = verify_wheel_modules(wheel, Path(sys.prefix) / "Lib" / "site-packages")
        os.replace(incoming, target)
        report.update({"state": "succeeded", "succeeded": True, "target": str(target), "manifest": manifest})
        save_status(report)
        return 0
    except Exception as exc:
        report.update({"state": "failed", "error_code": str(exc)[:128]})
        save_status(report)
        return 2
    finally:
        if incoming is not None and incoming.exists():
            shutil.rmtree(incoming, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
