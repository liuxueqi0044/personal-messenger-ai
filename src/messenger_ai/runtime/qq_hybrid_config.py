"""Pure opt-in configuration for the certified Windows QQ V2 surface.

Parse only the trusted ``qq_hybrid_v2`` section. Its absence means the runner
has not selected V2; this parser never enables it implicitly. PID/HWND/start,
profile anchors and labels must come from trusted deployment configuration,
not navigation-model output. Validation performs no filesystem, UI or DB I/O.
The runner separately checks session_epoch against its validated session
revision and supplies business binding_revisions from RuntimeState.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import PureWindowsPath
from types import MappingProxyType
from typing import Annotated, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator

from messenger_ai.adapters.qq.models import QQIdentityBinding, QQSelector, QQSelectorPack, QQWindow
from messenger_ai.adapters.qq.navigation.contracts import ContactTarget, NavigationModel
from messenger_ai.adapters.qq.navigation.identity import ProfileIdentityExpectation
from messenger_ai.adapters.qq.navigation.windows_backend import WindowsNavigationConfig
from messenger_ai.adapters.qq.vm_driver.selectors import validate_guest_selector_pack
from messenger_ai.llm.deepseek import DEEPSEEK_RESPONSES_BASE_URL


_Scope = Annotated[str, Field(min_length=1, max_length=128, strict=True)]
_Path = Annotated[str, Field(min_length=1, max_length=4096, strict=True)]


def _scope(value: str) -> str:
    if not value.strip() or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("hybrid scope must be nonempty single-line data")
    return value


def _absolute_local_path(value: str) -> str:
    # Validate Windows syntax even when offline tests run on another OS. Never
    # resolve, create, expand environment variables or inspect the target.
    path = PureWindowsPath(value)
    if (any(ord(c) < 32 or ord(c) == 127 for c in value)
            or not path.is_absolute() or len(path.drive) != 2 or path.drive[1] != ":"
            or ".." in path.parts or any(any(c in part for c in '<>:"|?*')
                                        or part.endswith((" ", ".")) for part in path.parts[1:])):
        raise ValueError("hybrid paths must be absolute local Windows paths without traversal")
    return str(path)


class _FrozenWindow(QQWindow):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @field_validator("class_name")
    @classmethod
    def _certified_class(cls, value):
        if value != "Chrome_WidgetWin_1":
            raise ValueError("hybrid window is not the certified QQ main-window class")
        return value


class HybridNavigationModelSettings(NavigationModel):
    model: str = Field(default="deepseek-flash", min_length=1, max_length=256, strict=True)
    endpoint: str = Field(default=DEEPSEEK_RESPONSES_BASE_URL, strict=True)
    timeout_seconds: float = Field(default=15, gt=0, le=30, strict=True, allow_inf_nan=False)
    max_output_tokens: int = Field(default=512, ge=32, le=512, strict=True)
    reasoning_effort: Literal["none", "low", "medium", "high"] = "none"
    schema_dialect: Literal["openai", "typed_nullable", "flat_primitive"] = "flat_primitive"

    _model_name = field_validator("model")(_scope)

    @field_validator("endpoint")
    @classmethod
    def _production_endpoint(cls, value):
        url = urlsplit(value)
        if (url.scheme != "https" or url.netloc != "api.deepseek.com"
                or url.path not in ("", "/") or url.query or url.fragment
                or "?" in value or "#" in value or value != value.strip()
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("hybrid navigation requires the certified DeepSeek HTTPS endpoint")
        return DEEPSEEK_RESPONSES_BASE_URL


class _BindingInput(NavigationModel):
    contact_id: str = Field(min_length=1, max_length=256, strict=True)
    target: ContactTarget
    expectation: ProfileIdentityExpectation


class _Input(NavigationModel):
    schema_version: Literal["qq_hybrid_runtime_v2"]
    enabled: Literal[True]
    helper_path: _Path
    vault_path: _Path
    guard_directory: _Path
    window: _FrozenWindow
    process_started_at_100ns: int = Field(gt=0, strict=True)
    session_epoch: _Scope
    surface_epoch: _Scope
    max_seconds: Literal[45] = 45
    prepare_write_reserve_seconds: Literal[20] = 20
    navigation_model: HybridNavigationModelSettings = Field(default_factory=HybridNavigationModelSettings)
    inputs: tuple[_BindingInput, ...] = Field(min_length=1)

    _paths = field_validator("helper_path", "vault_path", "guard_directory")(_absolute_local_path)
    _epochs = field_validator("session_epoch", "surface_epoch")(_scope)

    @field_validator("enabled", mode="before")
    @classmethod
    def _explicit_opt_in(cls, value):
        if type(value) is not bool:
            raise ValueError("hybrid opt-in must be a boolean")
        return value

    @field_validator("max_seconds", "prepare_write_reserve_seconds", mode="before")
    @classmethod
    def _fixed_integer_budget(cls, value):
        if type(value) is not int:
            raise ValueError("hybrid budgets must be fixed integer seconds")
        return value


@dataclass(frozen=True)
class QQHybridSettings:
    helper_path: str
    vault_path: str
    guard_directory: str
    window: QQWindow
    process_started_at_100ns: int
    session_epoch: str
    surface_epoch: str
    run_id: str
    max_seconds: int
    prepare_write_reserve_seconds: int
    navigation_model: HybridNavigationModelSettings
    targets: Mapping[str, ContactTarget]
    expectations: Mapping[str, ProfileIdentityExpectation]
    contact_ids: Mapping[str, str]
    # JSON snapshots keep the mutable legacy selector models out of frozen
    # settings. Every worker receives its own independently reconstructed copy.
    _composer_json: str = field(repr=False)
    _messages_json: str = field(repr=False)

    def navigation_config(self, *, worker_epoch: str | UUID, guard_state_path: str) -> WindowsNavigationConfig:
        if not isinstance(worker_epoch, (str, UUID)) or UUID(str(worker_epoch)).int == 0:
            raise ValueError("hybrid worker epoch must be a nonzero UUID")
        if not isinstance(guard_state_path, str):
            raise ValueError("hybrid guard path must be an absolute string")
        guard_path = PureWindowsPath(_absolute_local_path(guard_state_path))
        # The round factory generates the filename. No model-supplied path or
        # parent escape can replace a different runtime's guard publication.
        if guard_path.parent != PureWindowsPath(self.guard_directory) or guard_path.suffix.lower() != ".json":
            raise ValueError("hybrid guard file must be JSON directly inside guard_directory")
        def selector(name, control_type, tokens=(), **kwargs):
            return QQSelector(name=name, control_type=control_type, class_name_tokens=tokens, **kwargs)
        return WindowsNavigationConfig(guard_state_path=str(guard_path),
            window=QQWindow.model_validate(self.window.model_dump()),
            expected_process_started_at_100ns=self.process_started_at_100ns,
            expected_run_id=self.run_id, expected_worker_epoch=str(UUID(str(worker_epoch))),
            list_selector=selector("contact_list", "PaneControl", ("recent-contact-list",)),
            row_selector=selector("contact_row", "GroupControl", ("recent-contact-item",),
                                  selected_class_name_token="recent-contact-item--selected"),
            name_container_selector=selector("contact_info", "GroupControl", ("item__info",)),
            name_selector=selector("contact_label", "TextControl", class_name=""),
            search_container_selector=selector("search_container", "GroupControl", ("q-search__input",)),
            search_selector=selector("search", "EditControl", class_name=""),
            header_selector=selector("active_header", "ButtonControl", ("chat-header__contact-name",)),
            composer_selector=QQSelector.model_validate_json(self._composer_json),
            message_selector=QQSelector.model_validate_json(self._messages_json))


def parse_hybrid_settings(raw, *, selector_pack: QQSelectorPack,
        bindings: Sequence[QQIdentityBinding], binding_revisions: Mapping[str, int],
        run_id: str) -> QQHybridSettings:
    """Validate one complete opt-in section without enrolling any identity.

    The returned mappings are keyed by existing binding_id. No name, HMAC or
    UI locator is used to infer missing business identity/revision fields.
    """
    if not isinstance(raw, Mapping):
        raise ValueError("qq_hybrid_v2 must be an explicit configuration object")
    # Reparse value data so a caller passing an already-constructed Pydantic
    # object cannot bypass nested validation with model_copy/model_construct.
    config = _Input.model_validate(_Input.model_validate(dict(raw)).model_dump())
    if not isinstance(run_id, str) or not 1 <= len(run_id) <= 128:
        raise ValueError("hybrid run_id is invalid")
    _scope(run_id)
    if not isinstance(selector_pack, QQSelectorPack):
        raise ValueError("hybrid selector_pack must already be validated")
    pack = QQSelectorPack.model_validate(selector_pack.model_dump())
    validate_guest_selector_pack(pack)
    main_class = pack.selector("main_window").class_name
    if main_class is not None and main_class != config.window.class_name:
        raise ValueError("hybrid window disagrees with the certified selector pack")
    if (not isinstance(bindings, Sequence) or not bindings
            or any(not isinstance(item, QQIdentityBinding) for item in bindings)):
        raise ValueError("hybrid requires existing typed bindings")
    existing = tuple(QQIdentityBinding.model_validate(item.model_dump()) for item in bindings)
    by_id = {item.binding_id: item for item in existing}
    if (len(by_id) != len(existing)
            or len({item.hub_conversation_id for item in existing}) != len(existing)
            or len({item.contact_id for item in existing}) != len(existing)
            or any(item.conversation_type != "direct" or item.participant_signature.startswith("uncertified:")
                   for item in existing)):
        raise ValueError("hybrid requires unique certified direct business bindings")
    if (not isinstance(binding_revisions, Mapping) or set(binding_revisions) != set(by_id)
            or any(type(value) is not int or value < 1 for value in binding_revisions.values())):
        raise ValueError("hybrid binding revisions must exactly match existing bindings")
    inputs = {item.target.binding_id: item for item in config.inputs}
    if len(inputs) != len(config.inputs) or set(inputs) != set(by_id):
        raise ValueError("hybrid inputs must cover every existing binding exactly once")
    targets, expectations, contacts = {}, {}, {}
    for binding_id, binding in by_id.items():
        item = inputs[binding_id]
        target, expectation = item.target, item.expectation
        if (item.contact_id != binding.contact_id
                or target.account_id != binding.account_id
                or target.conversation_id != binding.hub_conversation_id
                or target.identity_mode != "persistent"
                or target.binding_revision != binding_revisions[binding_id]
                or any(getattr(target, key) != getattr(expectation, key) for key in
                       ("account_id", "conversation_id", "binding_id", "binding_revision"))
                or expectation.client_version != pack.client_version
                or expectation.selector_pack_version != pack.fixture_suite_version
                or expectation.environment_fingerprint != pack.environment_fingerprint):
            raise ValueError("hybrid target/profile scope disagrees with the existing business binding")
        targets[binding_id], expectations[binding_id], contacts[binding_id] = target, expectation, item.contact_id
    return QQHybridSettings(helper_path=config.helper_path, vault_path=config.vault_path,
        guard_directory=config.guard_directory, window=config.window,
        process_started_at_100ns=config.process_started_at_100ns,
        session_epoch=config.session_epoch, surface_epoch=config.surface_epoch, run_id=run_id,
        max_seconds=config.max_seconds, prepare_write_reserve_seconds=config.prepare_write_reserve_seconds,
        navigation_model=config.navigation_model, targets=MappingProxyType(targets),
        expectations=MappingProxyType(expectations), contact_ids=MappingProxyType(contacts),
        _composer_json=pack.selector("composer").model_dump_json(),
        _messages_json=pack.selector("bubbles").model_dump_json())


__all__ = ["QQHybridSettings", "HybridNavigationModelSettings", "parse_hybrid_settings"]
