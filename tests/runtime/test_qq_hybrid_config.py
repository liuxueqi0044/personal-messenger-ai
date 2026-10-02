"""Synthetic trusted deployment configuration; no QQ, secrets or DB needed."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.models import QQIdentityBinding, QQSelector, QQSelectorPack
from messenger_ai.adapters.qq.navigation.identity import ProfileIdentityExpectation
from messenger_ai.runtime.qq_hybrid_config import parse_hybrid_settings


def configuration():
    """Runnable sample section. IDs, PID/HWND, anchors and paths are synthetic."""
    pack = QQSelectorPack(client_version="synthetic-QQ-9", environment_fingerprint="e" * 64,
        fixture_suite_version="synthetic-selectors-v2", last_verified_at=datetime(2026, 10, 2, tzinfo=UTC),
        selectors=tuple(QQSelector(name=name, control_type=kind, class_name=cls) for name, kind, cls in (
            ("main_window", "WindowControl", "Chrome_WidgetWin_1"),
            ("conversations", "PaneControl", "legacy-list"),
            ("conversation_item", "GroupControl", "legacy-item"),
            ("composer", "DocumentControl", "ck-content"),
            ("send", "ButtonControl", "send-msg"),
            ("bubbles", "PaneControl", "ml-root"))))
    bindings = tuple(QQIdentityBinding(binding_id=f"binding-{n}", account_id="account-example",
        contact_id=f"contact-{n}", hub_conversation_id=f"conversation-{n}",
        platform_conversation_id=f"runtime:old-locator-{n}", conversation_type="direct",
        participant_signature=f"qq-session-observed:old-business-signature-{n}") for n in (1, 2))
    raw = {
        "schema_version": "qq_hybrid_runtime_v2", "enabled": True,
        "helper_path": r"C:\PMAI\tools\qq-ui-helper.exe",
        "vault_path": r"C:\PMAI\secrets\vault.json",
        "guard_directory": r"C:\PMAI\runtime\guards",
        "window": {"process_id": 1234, "window_handle": 5678, "class_name": "Chrome_WidgetWin_1"},
        "process_started_at_100ns": 134_000_000_000_000_000,
        "session_epoch": "2", "surface_epoch": "surface-example",
        "max_seconds": 45, "prepare_write_reserve_seconds": 20,
        "navigation_model": {"model": "deepseek-flash", "endpoint": "https://api.deepseek.com",
            "timeout_seconds": 15, "max_output_tokens": 512,
            "reasoning_effort": "none", "schema_dialect": "flat_primitive"},
        "inputs": [],
    }
    for n, binding in enumerate(bindings, 1):
        scope = {"binding_id": binding.binding_id, "account_id": binding.account_id,
                 "conversation_id": binding.hub_conversation_id, "binding_revision": 1}
        raw["inputs"].append({"contact_id": binding.contact_id,
            "target": {**scope, "display_name": f"Example Contact {n}", "search_aliases": [f"Alias {n}"],
                       "identity_mode": "persistent", "conversation_type": "direct"},
            "expectation": {**scope, "expected_profile_hmac": str(n) * 64,
                "hmac_key_id": "qq.identity.hmac.v1", "client_version": pack.client_version,
                "selector_pack_version": pack.fixture_suite_version,
                "environment_fingerprint": pack.environment_fingerprint}})
    return raw, pack, bindings


def parse(raw=None, *, pack=None, bindings=None, revisions=None, run_id="run-example"):
    sample, default_pack, default_bindings = configuration()
    return parse_hybrid_settings(sample if raw is None else raw, selector_pack=pack or default_pack,
        bindings=default_bindings if bindings is None else bindings,
        binding_revisions={"binding-1": 1, "binding-2": 1} if revisions is None else revisions, run_id=run_id)


def test_complete_settings_preserve_business_identity_and_separate_revision_domains():
    raw, pack, bindings = configuration()
    original = deepcopy(raw)
    binding_json = [item.model_dump_json() for item in bindings]
    settings = parse(raw, pack=pack, bindings=bindings)
    assert settings.session_epoch == "2"
    assert {item.binding_revision for item in settings.targets.values()} == {1}
    assert set(settings.targets) == set(settings.expectations) == {"binding-1", "binding-2"}
    assert settings.contact_ids == {"binding-1": "contact-1", "binding-2": "contact-2"}
    assert settings.expectations["binding-1"].expected_profile_hmac == "1" * 64
    assert [item.model_dump_json() for item in bindings] == binding_json
    assert raw == original
    assert settings.max_seconds == 45 and settings.prepare_write_reserve_seconds == 20
    assert settings.run_id == "run-example"


def test_default_navigation_provider_is_the_tested_bounded_configuration():
    raw, _, _ = configuration()
    del raw["navigation_model"]
    assert parse(raw).navigation_model.model_dump() == {
        "model": "deepseek-flash", "endpoint": "https://api.deepseek.com", "timeout_seconds": 15,
        "max_output_tokens": 512, "reasoning_effort": "none", "schema_dialect": "flat_primitive"}


def test_semantic_template_uses_pack_composer_and_bubbles_but_never_legacy_row_locators():
    raw, pack, bindings = configuration()
    settings = parse(raw, pack=pack, bindings=bindings)
    epoch = uuid4()
    config = settings.navigation_config(worker_epoch=epoch, guard_state_path=settings.guard_directory + r"\round.json")
    assert config.expected_worker_epoch == str(epoch)
    assert config.expected_run_id == settings.run_id
    assert config.window.model_dump() == settings.window.model_dump()
    assert config.expected_process_started_at_100ns == raw["process_started_at_100ns"]
    assert config.list_selector.class_name_tokens == ("recent-contact-list",)
    assert config.row_selector.class_name_tokens == ("recent-contact-item",)
    assert config.row_selector.selected_class_name_token == "recent-contact-item--selected"
    assert config.name_container_selector.class_name_tokens == ("item__info",)
    assert config.name_selector.control_type == "TextControl" and config.name_selector.class_name == ""
    assert config.search_container_selector.class_name_tokens == ("q-search__input",)
    assert config.search_selector.control_type == "EditControl" and config.search_selector.class_name == ""
    assert config.header_selector.class_name_tokens == ("chat-header__contact-name",)
    assert config.header_selector.control_type == "ButtonControl"
    assert config.composer_selector == pack.selector("composer")
    assert config.message_selector == pack.selector("bubbles")


def test_settings_and_nested_maps_are_frozen_and_detached_from_mutable_inputs():
    raw, pack, bindings = configuration()
    settings = parse(raw, pack=pack, bindings=bindings)
    with pytest.raises(FrozenInstanceError):
        settings.run_id = "other"
    with pytest.raises(TypeError):
        settings.targets["binding-1"] = settings.targets["binding-2"]
    with pytest.raises(ValidationError):
        settings.window.process_id = 999
    with pytest.raises(ValidationError):
        settings.targets["binding-1"].display_name = "other"
    raw["window"]["process_id"] = 999
    raw["inputs"][0]["target"]["display_name"] = "other"
    pack.selector("composer").class_name = "other"
    first = settings.navigation_config(worker_epoch=uuid4(), guard_state_path=settings.guard_directory + r"\first.json")
    first.window.process_id = 888
    first.composer_selector.class_name = "other"
    second = settings.navigation_config(worker_epoch=uuid4(), guard_state_path=settings.guard_directory + r"\second.json")
    assert second.window.process_id == 1234
    assert second.composer_selector.class_name == "ck-content"
    assert settings.targets["binding-1"].display_name == "Example Contact 1"


@pytest.mark.parametrize("field,value", [
    ("enabled", False), ("enabled", 1), ("enabled", "true"),
    ("schema_version", "qq_hybrid_runtime_v1"),
    ("process_started_at_100ns", True), ("process_started_at_100ns", "123"),
    ("process_started_at_100ns", 0), ("session_epoch", 2), ("session_epoch", ""),
    ("surface_epoch", "\n"), ("surface_epoch", " x "),
    ("max_seconds", 46), ("max_seconds", 44), ("max_seconds", 45.0),
    ("prepare_write_reserve_seconds", 19), ("prepare_write_reserve_seconds", "20"),
    ("inputs", []), ("inputs", {}), ("inputs", "binding-1"),
    ("model_api_key", "must-not-accept-secrets"), ("run_id", "untrusted-run"),
    ("row_selector", {"runtime_id": [1, 2]}),
])
def test_rejects_implicit_opt_in_types_and_unapproved_fields(field, value):
    raw, _, _ = configuration()
    raw[field] = value
    with pytest.raises(ValueError):
        parse(raw)


@pytest.mark.parametrize("field", ["enabled", "schema_version", "helper_path", "vault_path", "guard_directory",
    "window", "process_started_at_100ns", "session_epoch", "surface_epoch", "inputs"])
def test_no_inferred_missing_production_values(field):
    raw, _, _ = configuration()
    del raw[field]
    with pytest.raises(ValueError):
        parse(raw)


@pytest.mark.parametrize("path", ["relative.exe", r"C:relative.exe", r"\rooted.exe", r"\\server\share\tool.exe",
    r"C:\tools\..\other.exe", "C:\\tools\\bad\x00.exe", "https://example.com/tool.exe", 12,
    r"C:\tools\stream.exe:alternate", r"C:\tools\*.exe", r"C:\tools\trailing.\helper.exe"])
@pytest.mark.parametrize("field", ["helper_path", "vault_path", "guard_directory"])
def test_paths_are_explicit_local_absolute_and_never_expanded(field, path):
    raw, _, _ = configuration()
    raw[field] = path
    with pytest.raises(ValueError):
        parse(raw)


@pytest.mark.parametrize("field,value", [("process_id", True), ("process_id", "1234"),
    ("window_handle", 0), ("window_handle", 5678.0), ("class_name", "OtherWindow"), ("runtime_id", [1])])
def test_window_requires_explicit_certified_metadata(field, value):
    raw, _, _ = configuration()
    raw["window"][field] = value
    with pytest.raises(ValueError):
        parse(raw)


@pytest.mark.parametrize("endpoint", ["http://api.deepseek.com", "https://elsewhere.example", "https://api.deepseek.com.evil",
    "https://key@api.deepseek.com", "https://api.deepseek.com:443", "https://api.deepseek.com/v1",
    "https://api.deepseek.com?secret=x", "https://api.deepseek.com#x", "https://api.deepseek.com?",
    "https://api.deepseek.com#", "https://api.deepseek.com\n", "https://api.deep\nseek.com", "https://api.deepseek.com\\evil"])
def test_endpoint_is_the_production_domain_without_routing_or_secret_overrides(endpoint):
    raw, _, _ = configuration()
    raw["navigation_model"]["endpoint"] = endpoint
    with pytest.raises(ValueError):
        parse(raw)


@pytest.mark.parametrize("field,value", [("model", ""), ("model", "bad\nname"), ("model", 12),
    ("max_output_tokens", 513), ("max_output_tokens", True), ("max_output_tokens", "512"),
    ("timeout_seconds", True), ("timeout_seconds", float("inf")), ("timeout_seconds", 31),
    ("timeout_seconds", "15"), ("reasoning_effort", "max"), ("schema_dialect", "free_text"),
    ("api_key", "not-allowed"), ("max_retries", 5)])
def test_provider_configuration_has_closed_types_and_budgets(field, value):
    raw, _, _ = configuration()
    raw["navigation_model"][field] = value
    with pytest.raises(ValueError):
        parse(raw)


@pytest.mark.parametrize("where,field,value", [
    ("target", "account_id", "other"), ("target", "conversation_id", "other"),
    ("target", "binding_id", "other"), ("target", "binding_revision", 2),
    ("target", "binding_revision", True), ("target", "identity_mode", "session_bound"),
    ("target", "conversation_type", "group"), ("target", "display_name", "\n"),
    ("target", "search_aliases", ["alias", "alias"]), ("target", "search_aliases", [4]),
    ("target", "participant_signature", "replacement"), ("target", "selected_runtime_id", [1, 2]),
    ("expectation", "account_id", "other"), ("expectation", "conversation_id", "other"),
    ("expectation", "binding_id", "binding-2"), ("expectation", "binding_revision", 2),
    ("expectation", "expected_profile_hmac", "not-a-hash"),
    ("expectation", "client_version", "other"), ("expectation", "selector_pack_version", "other"),
    ("expectation", "environment_fingerprint", "f" * 64), ("expectation", "hmac_key_id", ""),
])
def test_targets_and_profile_anchors_cannot_change_scope(where, field, value):
    raw, _, _ = configuration()
    raw["inputs"][0][where][field] = value
    with pytest.raises(ValueError):
        parse(raw)


@pytest.mark.parametrize("change", ["contact", "missing", "duplicate", "extra", "missing_expectation"])
def test_every_business_binding_requires_exactly_one_target_and_profile_anchor(change):
    raw, _, _ = configuration()
    if change == "contact":
        raw["inputs"][0]["contact_id"] = "different-contact"
    elif change == "missing":
        raw["inputs"].pop()
    elif change == "duplicate":
        raw["inputs"][1] = deepcopy(raw["inputs"][0])
    elif change == "extra":
        raw["inputs"].append(deepcopy(raw["inputs"][0]))
        raw["inputs"][-1]["target"]["binding_id"] = "new-binding"
    else:
        del raw["inputs"][0]["expectation"]
    with pytest.raises(ValueError):
        parse(raw)


@pytest.mark.parametrize("revisions", [{}, {"binding-1": 1}, {"binding-1": 1, "binding-2": 1, "extra": 1},
    {"binding-1": True, "binding-2": 1}, {"binding-1": "1", "binding-2": 1},
    {"binding-1": 0, "binding-2": 1}, {"binding-1": 2, "binding-2": 1}, []])
def test_revisions_are_complete_current_business_revisions(revisions):
    with pytest.raises(ValueError):
        parse(revisions=revisions)


@pytest.mark.parametrize("field,value", [("conversation_type", "group"), ("participant_signature", "uncertified:test"),
    ("binding_id", "binding-1"), ("hub_conversation_id", "conversation-1"), ("contact_id", "contact-1")])
def test_legacy_binding_validation_remains_fail_closed(field, value):
    raw, pack, bindings = configuration()
    bindings[1].__setattr__(field, value)
    with pytest.raises(ValueError):
        parse(raw, pack=pack, bindings=bindings)


@pytest.mark.parametrize("change", ["missing", "duplicate", "window_mismatch"])
def test_reuses_guest_selector_pack_completeness_validation(change):
    raw, pack, bindings = configuration()
    if change == "missing":
        pack.selectors = tuple(x for x in pack.selectors if x.name != "send")
    elif change == "duplicate":
        pack.selectors += (pack.selectors[0],)
    else:
        pack.selector("main_window").class_name = "OtherWindow"
    with pytest.raises(ValueError):
        parse(raw, pack=pack, bindings=bindings)


@pytest.mark.parametrize("run_id", [None, 123, "", " ", "\n", "x" * 129])
def test_run_id_must_be_a_trusted_bounded_scope(run_id):
    with pytest.raises(ValueError):
        parse(run_id=run_id)


@pytest.mark.parametrize("epoch", [None, 123, "not-a-uuid", str(UUID(int=0))])
def test_worker_epoch_must_be_fresh_explicit_uuid(epoch):
    settings = parse()
    with pytest.raises(ValueError):
        settings.navigation_config(worker_epoch=epoch, guard_state_path=settings.guard_directory + r"\round.json")


@pytest.mark.parametrize("path", [r"C:\PMAI\runtime\elsewhere\round.json", r"C:\PMAI\runtime\guards-evil\round.json",
    r"C:\PMAI\runtime\guards\nested\round.json", r"C:\PMAI\runtime\guards\..\round.json",
    r"C:\PMAI\runtime\guards", r"C:\PMAI\runtime\guards\round.exe", "round.json", 4])
def test_guard_file_is_bound_to_the_configured_directory(path):
    with pytest.raises(ValueError):
        parse().navigation_config(worker_epoch=uuid4(), guard_state_path=path)


def test_parser_performs_no_file_io_or_changes_to_persistent_state(monkeypatch):
    import builtins
    import pathlib
    import sqlite3
    def forbidden(*args, **kwargs):
        pytest.fail("pure config parser touched filesystem or SQLite")
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(pathlib.Path, "open", forbidden)
        patch.setattr(pathlib.Path, "resolve", forbidden)
        patch.setattr(sqlite3, "connect", forbidden)
        settings = parse()
        settings.navigation_config(worker_epoch=uuid4(), guard_state_path=settings.guard_directory + r"\test.json")


@pytest.mark.parametrize("raw", [None, False, "{}", [], 123])
def test_parser_never_treats_absent_or_nonobject_sections_as_opt_in(raw):
    _, pack, bindings = configuration()
    with pytest.raises(ValueError):
        parse_hybrid_settings(raw, selector_pack=pack, bindings=bindings,
            binding_revisions={"binding-1": 1, "binding-2": 1}, run_id="run-example")


def test_preconstructed_models_cannot_bypass_anchor_validation():
    raw, _, _ = configuration()
    expectation = ProfileIdentityExpectation.model_validate(raw["inputs"][0]["expectation"])
    raw["inputs"][0]["expectation"] = expectation.model_copy(update={"expected_profile_hmac": "invalid"})
    with pytest.raises(ValueError):
        parse(raw)
