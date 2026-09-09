from __future__ import annotations

from messenger_ai.adapters.qq.models import QQIdentityBinding, QQSelectorPack

from .transport import WindowsUIAQQAccessibility
from .worker import QQVMWorker


def assemble_guest_worker(*, selector_pack: QQSelectorPack, bindings: tuple[QQIdentityBinding, ...]) -> QQVMWorker:
    """The only production assembly entry point for the concrete guest driver."""
    return QQVMWorker(accessibility=WindowsUIAQQAccessibility(), selector_pack=selector_pack, bindings=bindings)
