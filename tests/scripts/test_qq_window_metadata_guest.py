import importlib.util
from pathlib import Path


PATH = Path(__file__).parents[2] / "scripts" / "deployment" / "qq_window_metadata_guest.py"
SPEC = importlib.util.spec_from_file_location("qq_window_metadata_guest", PATH)
assert SPEC and SPEC.loader
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_safe_diagnostic_contains_location_but_no_exception_text_or_locals() -> None:
    secret = "must-not-leak"
    try:
        int(None)
    except TypeError as exc:
        value = module._safe_error("metadata_collection_failed", exc)
    rendered = repr(value)
    assert value["diagnostic"]["exception_type"] == "TypeError"
    assert value["diagnostic"]["frames"][-1]["function"] == (
        "test_safe_diagnostic_contains_location_but_no_exception_text_or_locals"
    )
    assert secret not in rendered
    assert "NoneType" not in rendered
