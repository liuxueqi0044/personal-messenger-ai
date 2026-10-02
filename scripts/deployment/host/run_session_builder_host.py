"""Run the frozen session config builder through a background GuestSession."""
from __future__ import annotations

import argparse
import functools
import hashlib
import importlib.util
import json
import re
import time
import uuid
import msvcrt
from pathlib import Path
from typing import Any


ROOT = Path(r"C:\Users\xiayu\Documents\Codex\2026-09-09\new-chat")
CONTROL = ROOT / r"outputs\qq-vm\install\control_qq_runtime_host.py"
DIAG = ROOT / r"outputs\qq-vm\install\diagnostics"
GUEST_PYTHON = r"C:\PMAI\app\.venv\Scripts\python.exe"
GUEST_OUTPUT = r"C:\PMAI\data\runtime-session-1.json"
WRAPPER_HOST = ROOT / r"outputs\qq-vm\install\run_session_builder_guest_wrapper.py"
GUEST_WRAPPER = r"C:\PMAI\data\run-session-builder-guest-wrapper.py"
HOST_BUILD_LOCK = DIAG / ".session-builder.lock"


def _exclusive_host_build(function):
    @functools.wraps(function)
    def wrapped() -> int:
        DIAG.mkdir(parents=True, exist_ok=True)
        with HOST_BUILD_LOCK.open("a+b") as lock:
            lock.seek(0)
            if lock.tell() == 0 and lock.read(1) == b"":
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            try:
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("SESSION_BUILDER_ALREADY_RUNNING") from exc
            try:
                return function()
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    return wrapped


def _control_module() -> Any:
    spec = importlib.util.spec_from_file_location("pmai_builder_control", CONTROL)
    if spec is None or spec.loader is None:
        raise RuntimeError("CONTROL_HELPER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def _terminate_guest_process(process: Any, *, prefix: str) -> None:
    try:
        process.Terminate()
    except Exception as exc:
        raise RuntimeError(f"{prefix}_TERMINATION_FAILED") from exc
    if process.WaitForArray([2, 4], 10_000) != 2:
        raise RuntimeError(f"{prefix}_TERMINATION_UNCONFIRMED")


def _run_guest(
    guest: Any, release_id: str, indices: list[int], header_upgrades: list[int],
    adoption_indices: list[int], isolated_generation: str | None,
    visual_labels: list[str], guest_result: str,
) -> tuple[int, int]:
    args = [
        GUEST_PYTHON,
        GUEST_WRAPPER,
        "--release-id", release_id,
        "--result", guest_result,
    ]
    for index in indices:
        args.extend(("--contact-index", str(index)))
    for index in header_upgrades:
        args.extend(("--migrate-header-digest-index", str(index)))
    for index in adoption_indices:
        args.extend(("--adopt-latest-inbound-index", str(index)))
    if isolated_generation is not None:
        args.extend(("--isolated-recovery-generation", isolated_generation))
    for visual_label in visual_labels:
        args.extend(("--visual-label", visual_label))
    process = guest.ProcessCreate(
        GUEST_PYTHON, args, r"C:\PMAI\data", [], [], 120_000
    )
    if process.WaitForArray([1], 30_000) != 1:
        _terminate_guest_process(process, prefix="BUILDER_PROCESS")
        raise RuntimeError("BUILDER_PROCESS_DID_NOT_START")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = process.WaitForArray([2, 4], 500)
        if state == 2:
            return int(process.Status), int(process.ExitCode)
        if state == 4:
            break
    error = "BUILDER_PROCESS_WAIT_ERROR" if state == 4 else "BUILDER_PROCESS_TIMEOUT"
    _terminate_guest_process(process, prefix="BUILDER_PROCESS")
    raise RuntimeError(error)


def _run_guest_check(
    guest: Any, release_id: str, expected_config_sha256: str
) -> int:
    runtime = rf"C:\PMAI\app\releases\{release_id}\run_vm_runtime.py"
    process = guest.ProcessCreate(
        GUEST_PYTHON,
        [
            GUEST_PYTHON,
            runtime,
            "--config",
            GUEST_OUTPUT,
            "--check",
            "--expected-config-sha256",
            expected_config_sha256,
        ],
        r"C:\PMAI\data",
        [],
        [],
        120_000,
    )
    if process.WaitForArray([1], 30_000) != 1:
        _terminate_guest_process(process, prefix="RUNTIME_CHECK")
        raise RuntimeError("RUNTIME_CHECK_DID_NOT_START")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        state = process.WaitForArray([2, 4], 500)
        if state == 2:
            return int(process.ExitCode)
        if state == 4:
            break
    _terminate_guest_process(process, prefix="RUNTIME_CHECK")
    raise RuntimeError(
        "RUNTIME_CHECK_WAIT_ERROR" if state == 4 else "RUNTIME_CHECK_TIMEOUT"
    )


def _guest_atomic_rollback(
    guest: Any,
    control: Any,
    backup: Path,
    *,
    expected_current_sha256: str,
    backup_sha256: str,
    run_id: str,
) -> None:
    staged = rf"C:\PMAI\data\.runtime-session-1-rollback-{run_id}.json"
    control._wait_progress(
        guest.FileCopyToGuest(str(backup), staged, []), 30_000
    )
    program = """import ctypes,getpass,hashlib,os,sys
target,staged,expected,backup=sys.argv[1:]
identity='\\\\'.join(part for part in (os.environ.get('USERDOMAIN'),getpass.getuser()) if part)
name='Local\\\\PersonalMessengerAI.QQRuntime.'+hashlib.sha256(identity.casefold().encode('utf-8')).hexdigest()[:24]
kernel32=ctypes.WinDLL('kernel32',use_last_error=True)
kernel32.CreateMutexW.argtypes=(ctypes.c_void_p,ctypes.c_int,ctypes.c_wchar_p)
kernel32.CreateMutexW.restype=ctypes.c_void_p
ctypes.set_last_error(0)
handle=kernel32.CreateMutexW(None,True,name)
if not handle:
    sys.exit(7)
if ctypes.get_last_error()==183:
    kernel32.CloseHandle(handle)
    sys.exit(6)
try:
    digest=lambda p:hashlib.sha256(open(p,'rb').read()).hexdigest().upper()
    if digest(target)!=expected:
        sys.exit(4)
    if digest(staged)!=backup:
        sys.exit(5)
    os.replace(staged,target)
finally:
    kernel32.ReleaseMutex(handle)
    kernel32.CloseHandle(handle)
"""
    process = guest.ProcessCreate(
        GUEST_PYTHON,
        [
            GUEST_PYTHON,
            "-c",
            program,
            GUEST_OUTPUT,
            staged,
            expected_current_sha256,
            backup_sha256,
        ],
        r"C:\PMAI\data",
        [],
        [],
        30_000,
    )
    if process.WaitForArray([1], 10_000) != 1:
        _terminate_guest_process(process, prefix="ROLLBACK_PROCESS")
        raise RuntimeError("ROLLBACK_PROCESS_DID_NOT_START")
    rollback_state = process.WaitForArray([2, 4], 30_000)
    if rollback_state != 2:
        _terminate_guest_process(process, prefix="ROLLBACK_PROCESS")
        raise RuntimeError("ROLLBACK_CONFIG_CAS_FAILED")
    if int(process.ExitCode) != 0:
        raise RuntimeError("ROLLBACK_CONFIG_CAS_FAILED")


@_exclusive_host_build
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument(
        "--contact-index", type=int, action="append", required=True,
        help=(
            "exact selected contacts in isolated mode; otherwise contacts to "
            "refresh under the existing compatibility/discovery rules"
        ),
    )
    parser.add_argument(
        "--migrate-header-digest-index", type=int, action="append", default=[]
    )
    parser.add_argument(
        "--adopt-latest-inbound-index", type=int, action="append", default=[]
    )
    parser.add_argument("--isolated-recovery-generation")
    parser.add_argument("--visual-label", action="append", default=[])
    args = parser.parse_args()
    if re.fullmatch(r"r\d{8}-\d{2}", args.release_id) is None:
        parser.error("release-id must use rYYYYMMDD-NN")
    indices = sorted(set(args.contact_index))
    header_upgrades = sorted(set(args.migrate_header_digest_index))
    adoption_indices = sorted(set(args.adopt_latest_inbound_index))
    if any(isinstance(index, bool) or not 1 <= index <= 9999 for index in indices):
        parser.error("contact-index must be between 1 and 9999")
    if set(header_upgrades) - set(indices):
        parser.error("header digest migration must target a refreshed contact")
    if set(adoption_indices) - set(indices):
        parser.error("latest inbound adoption must target a refreshed contact")
    isolated_generation = None
    if args.isolated_recovery_generation is not None:
        try:
            isolated_generation = str(uuid.UUID(args.isolated_recovery_generation))
        except ValueError:
            parser.error("isolated recovery generation must be a UUID")
        if isolated_generation != args.isolated_recovery_generation:
            parser.error("isolated recovery generation must be canonical")
    visual_indices: set[int] = set()
    for item in args.visual_label:
        index_text, separator, label = item.partition("=")
        if (
            separator != "="
            or not index_text.isdigit()
            or not label.strip()
            or len(label) > 96
            or any(ord(character) < 32 for character in label)
        ):
            parser.error("visual label must use INDEX=LABEL")
        index = int(index_text)
        if index in visual_indices:
            parser.error("visual label index must be unique")
        visual_indices.add(index)
    if visual_indices and visual_indices != set(indices):
        parser.error("visual labels must cover refreshed contacts one-to-one")

    run_id = uuid.uuid4().hex
    output = DIAG / f"runtime-session-1-{args.release_id}-{run_id}.json"
    backup = DIAG / f"runtime-session-1-before-{args.release_id}-{run_id}.json"
    rollback_probe = DIAG / f".runtime-session-1-rollback-{run_id}.json"
    wrapper_output = DIAG / f"session-builder-wrapper-{args.release_id}-{run_id}.json"
    manifest_output = DIAG / f"runtime-generation-manifest-{run_id}.json"
    generation_config_output = DIAG / f"runtime-generation-config-{run_id}.json"
    guest_wrapper_output = rf"C:\PMAI\data\session-builder-wrapper-{run_id}.json"
    report_path = DIAG / f"run-session-builder-{args.release_id}-{run_id}.json"
    report: dict[str, Any] = {
        "schema": "pmai-session-builder-host-v1",
        "release_id": args.release_id,
        "contact_indices": indices,
        "header_upgrade_indices": header_upgrades,
        "adopt_latest_inbound_indices": adoption_indices,
        "isolated_recovery_generation": isolated_generation,
        "succeeded": False,
    }
    control = guest = host = None
    clear = bytearray()
    builder_started = False
    wrapper: dict[str, Any] = {}
    candidate_config_sha256: str | None = None
    try:
        control = _control_module()
        _vbox, host, guest, clear, _password = control._open_guest()
        runtime = control._runtime_status(guest, f"builder-{run_id}")
        if runtime.get("runtime_process_alive") is not False:
            raise RuntimeError("RUNTIME_NOT_STOPPED")
        control._wait_progress(
            guest.FileCopyFromGuest(GUEST_OUTPUT, str(backup), []), 30_000
        )
        report["previous_config_sha256"] = _sha256(backup)
        control._wait_progress(
            guest.FileCopyToGuest(str(WRAPPER_HOST), GUEST_WRAPPER, []), 30_000
        )
        builder_started = True
        status, exit_code = _run_guest(
            guest,
            args.release_id,
            indices,
            header_upgrades,
            adoption_indices,
            isolated_generation,
            args.visual_label,
            guest_wrapper_output,
        )
        report.update({
            "process_status": status,
            "builder_exit_code": exit_code,
        })
        control._wait_progress(
            guest.FileCopyFromGuest(guest_wrapper_output, str(wrapper_output), []),
            30_000,
        )
        wrapper = json.loads(wrapper_output.read_text(encoding="utf-8-sig"))
        report["guest_wrapper"] = wrapper
        if exit_code != 0 or wrapper.get("succeeded") is not True:
            raise RuntimeError(str(wrapper.get("error_code") or "BUILDER_FAILED"))
        control._wait_progress(
            guest.FileCopyFromGuest(GUEST_OUTPUT, str(output), []), 30_000
        )
        config = json.loads(output.read_text(encoding="utf-8-sig"))
        candidate_config_sha256 = _sha256(output)
        evidence = config.get("session_observed_evidence")
        migrations = config.get("session_identity_migrations", [])
        visual = config.get("visual_selection")
        generation = config.get("runtime_generation")
        if isolated_generation is not None:
            if (
                not isinstance(generation, dict)
                or generation.get("schema")
                != "pmai-isolated-runtime-generation-v1"
                or generation.get("generation_id") != isolated_generation
                or generation.get("mode") != "isolated_identity_recovery"
                or generation.get("enforce_global_pause") is not True
                or re.fullmatch(
                    r"[0-9a-f]{64}", str(generation.get("manifest_sha256", ""))
                ) is None
                or config.get("start_globally_paused") is not True
                or str(config.get("data_dir", ""))
                != rf"C:\PMAI\data\runtime\recovery-generations\{isolated_generation}\qq-default-account"
            ):
                raise RuntimeError("ISOLATED_GENERATION_CONFIG_INVALID")
            guest_manifest = (
                str(config["data_dir"]).rstrip("\\/")
                + r"\generation-manifest.json"
            )
            control._wait_progress(
                guest.FileCopyFromGuest(guest_manifest, str(manifest_output), []),
                30_000,
            )
            manifest = json.loads(manifest_output.read_text(encoding="utf-8-sig"))
            manifest_contacts = manifest.get("contacts")
            manifest_by_binding = {
                item.get("binding_id"): item
                for item in manifest_contacts
                if isinstance(item, dict)
            } if isinstance(manifest_contacts, list) else {}
            expected_adoptions = {
                f"session-contact-{index}" for index in adoption_indices
            }
            actual_adoptions = {
                binding_id for binding_id, item in manifest_by_binding.items()
                if item.get("initial_adoption_sha256") is not None
            }
            if (
                _sha256(manifest_output).lower()
                != generation["manifest_sha256"]
                or manifest.get("schema")
                != "pmai-isolated-runtime-generation-manifest-v1"
                or manifest.get("generation_id") != isolated_generation
                or manifest.get("mode") != "isolated_identity_recovery"
                or manifest.get("account_id") != "qq-default-account"
                or not isinstance(manifest_contacts, list)
                or set(manifest_by_binding)
                != {f"session-contact-{index}" for index in indices}
                or actual_adoptions != expected_adoptions
                or migrations
            ):
                raise RuntimeError("ISOLATED_GENERATION_MANIFEST_INVALID")
            guest_generation_config = (
                str(config["data_dir"]).rstrip("/\\") + r"\runtime-config.json"
            )
            control._wait_progress(
                guest.FileCopyFromGuest(
                    guest_generation_config, str(generation_config_output), []
                ),
                30_000,
            )
            if _sha256(generation_config_output) != candidate_config_sha256:
                raise RuntimeError("ISOLATED_GENERATION_CONFIG_SNAPSHOT_MISMATCH")
            report["generation_manifest_sha256"] = _sha256(manifest_output)
            report["generation_manifest_path"] = str(manifest_output)
            report["generation_config_sha256"] = _sha256(generation_config_output)
        visual_labels = visual.get("labels") if isinstance(visual, dict) else None
        if (
            exit_code != 0
            or not isinstance(config, dict)
            or config.get("schema") != "pmai-v5-runtime-1"
            or not isinstance(evidence, list)
            or len(evidence) != len(indices)
            or not isinstance(migrations, list)
            or (
                bool(args.visual_label)
                and (
                    not isinstance(visual, dict)
                    or visual.get("model") != "deepseek-v4-flash-vision-exp"
                    or visual.get("min_confidence") != 0.98
                    or not isinstance(visual_labels, dict)
                    or set(visual_labels)
                    != {f"session-contact-{index}" for index in indices}
                )
            )
        ):
            raise RuntimeError("BUILDER_RESULT_INVALID")
        runtime_check_exit_code = _run_guest_check(
            guest, args.release_id, candidate_config_sha256
        )
        report["runtime_check_exit_code"] = runtime_check_exit_code
        if runtime_check_exit_code != 0:
            raise RuntimeError("RUNTIME_CONFIG_CHECK_FAILED")
        report.update({
            "config_sha256": candidate_config_sha256,
            "evidence_count": len(evidence),
            "migration_count": len(migrations),
            "binding_ids": sorted(str(item.get("binding_id")) for item in evidence),
            "visual_label_binding_ids": [
                f"session-contact-{index}" for index in sorted(visual_indices)
            ],
            "succeeded": True,
        })
        return 0
    except Exception as exc:
        report["error_code"] = str(exc)[:128]
        if builder_started and guest is not None and control is not None and backup.is_file():
            try:
                rollback_probe.unlink(missing_ok=True)
                control._wait_progress(
                    guest.FileCopyFromGuest(
                        GUEST_OUTPUT, str(rollback_probe), []
                    ),
                    30_000,
                )
                current_hash = _sha256(rollback_probe)
                backup_hash = _sha256(backup)
                wrapper_previous = wrapper.get("previous_config_sha256")
                wrapper_result = wrapper.get("result_config_sha256")
                if current_hash == backup_hash:
                    report["rollback_succeeded"] = True
                elif (
                    isinstance(wrapper_previous, str)
                    and wrapper_previous.upper() == backup_hash
                    and isinstance(wrapper_result, str)
                    and wrapper_result.upper() == current_hash
                    and (
                        candidate_config_sha256 is None
                        or candidate_config_sha256 == current_hash
                    )
                ):
                    _guest_atomic_rollback(
                        guest,
                        control,
                        backup,
                        expected_current_sha256=current_hash,
                        backup_sha256=backup_hash,
                        run_id=run_id,
                    )
                    rollback_probe.unlink(missing_ok=True)
                    control._wait_progress(
                        guest.FileCopyFromGuest(
                            GUEST_OUTPUT, str(rollback_probe), []
                        ),
                        30_000,
                    )
                    report["rollback_succeeded"] = (
                        _sha256(rollback_probe) == backup_hash
                    )
                else:
                    report["rollback_succeeded"] = False
                    report["rollback_refused_reason"] = "CONFIG_CAS_MISMATCH"
            except Exception:
                report["rollback_succeeded"] = False
        return 2
    finally:
        DIAG.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
        rollback_probe.unlink(missing_ok=True)
        for index in range(len(clear)):
            clear[index] = 0
        if guest is not None:
            try:
                guest.Close()
            except Exception:
                pass
        if host is not None:
            try:
                host.UnlockMachine()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
