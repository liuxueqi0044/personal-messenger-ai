from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock
from zipfile import ZipFile

import pytest

HOST_SCRIPTS = Path(__file__).parents[2] / "scripts" / "deployment" / "host"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


STAGE = _load_module(
    "stage_install_guest_release_host",
    HOST_SCRIPTS / "stage_install_guest_release_host.py",
)
INSTALLER = _load_module(
    "install_frozen_release_guest_v2",
    HOST_SCRIPTS / "install_frozen_release_guest_v2.py",
)


@pytest.mark.parametrize(
    "runtime",
    [
        pytest.param({}, id="state-missing"),
        pytest.param({"runtime_process_alive": None}, id="state-none"),
        pytest.param({"runtime_process_alive": True}, id="state-true"),
    ],
)
def test_assert_runtime_stopped_rejects_unproven_state(
    runtime: dict[str, object],
) -> None:
    start = Mock()

    with pytest.raises(RuntimeError, match="^RUNTIME_STOP_STATE_UNPROVEN$"):
        STAGE.assert_runtime_stopped(runtime, start, object())

    start._guest_pid_alive.assert_not_called()


def test_assert_runtime_stopped_rejects_missing_pid() -> None:
    start = Mock()

    with pytest.raises(RuntimeError, match="^RUNTIME_PID_UNAVAILABLE$"):
        STAGE.assert_runtime_stopped(
            {"runtime_process_alive": False},
            start,
            object(),
        )

    start._guest_pid_alive.assert_not_called()


def test_assert_runtime_stopped_rejects_live_pid() -> None:
    start = Mock()
    start._guest_pid_alive.return_value = True
    guest = object()

    with pytest.raises(RuntimeError, match="^RUNTIME_NOT_STOPPED$"):
        STAGE.assert_runtime_stopped(
            {"runtime_process_alive": False, "runtime_process_id": 4312},
            start,
            guest,
        )

    start._guest_pid_alive.assert_called_once_with(guest, 4312)


def test_assert_runtime_stopped_accepts_strict_false_and_dead_positive_pid() -> None:
    start = Mock()
    start._guest_pid_alive.return_value = False
    guest = object()

    result = STAGE.assert_runtime_stopped(
        {"runtime_process_alive": False, "runtime_process_id": 4312},
        start,
        guest,
    )

    assert result is None
    start._guest_pid_alive.assert_called_once_with(guest, 4312)


def _synthetic_wheel(
    tmp_path: Path,
    modules: dict[str, bytes],
) -> tuple[Path, Path]:
    wheel = tmp_path / "synthetic-0.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(
            "synthetic-0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: synthetic\nVersion: 0.0\n",
        )
        for name, payload in modules.items():
            archive.writestr(name, payload)

    installed_root = tmp_path / "site-packages"
    installed_root.mkdir()
    for name, payload in modules.items():
        installed = installed_root / Path(name)
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(payload)
    return wheel, installed_root


def test_verify_wheel_modules_checks_every_python_module(tmp_path: Path) -> None:
    modules = {
        "messenger_ai/__init__.py": b"PACKAGE = True\n",
        "messenger_ai/runtime/__init__.py": b"",
        "messenger_ai/runtime/worker.py": b"VALUE = 7\n",
    }
    wheel, installed_root = _synthetic_wheel(tmp_path, modules)

    verified = INSTALLER.verify_wheel_modules(wheel, installed_root)

    assert verified == {
        name: hashlib.sha256(payload).hexdigest().upper()
        for name, payload in modules.items()
    }


def test_verify_wheel_modules_rejects_any_tampered_installed_module(
    tmp_path: Path,
) -> None:
    modules = {
        "messenger_ai/__init__.py": b"PACKAGE = True\n",
        "messenger_ai/runtime/worker.py": b"VALUE = 7\n",
    }
    wheel, installed_root = _synthetic_wheel(tmp_path, modules)
    (installed_root / "messenger_ai" / "runtime" / "worker.py").write_bytes(
        b"VALUE = 'tampered'\n"
    )

    with pytest.raises(RuntimeError, match="^INSTALLED_MODULE_HASH_MISMATCH$"):
        INSTALLER.verify_wheel_modules(wheel, installed_root)


def test_verify_wheel_modules_rejects_empty_module_set(tmp_path: Path) -> None:
    wheel, installed_root = _synthetic_wheel(tmp_path, {})

    with pytest.raises(RuntimeError, match="^WHEEL_MODULE_SET_INVALID$"):
        INSTALLER.verify_wheel_modules(wheel, installed_root)
