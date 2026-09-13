from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).parents[2]
    / "scripts"
    / "deployment"
    / "qq_session_observed_bootstrap_guest.py"
)


def test_bootstrap_entrypoint_imports_and_maps_any_canonical_contact() -> None:
    name = "test_qq_session_observed_bootstrap_guest_module"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)

    assert module._output_for_binding("session-contact-37") == (
        Path(r"C:\PMAI\data") / "qq-session-observed-bootstrap-37.json"
    )
    with pytest.raises(ValueError, match="canonical"):
        module._output_for_binding("session-contact-03")
