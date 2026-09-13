"""QQ NT UI Automation driver, intended to run inside the Windows guest only."""

from .contracts import (
    SelectionHandoff,
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
    mint_selection_handoff,
)
from .transport import WindowsUIAQQAccessibility
from .worker import QQVMWorker, QQVMWorkerProcess
from .bridge import QQVMDriverBridge
from .message_cursor import MessageCursorStore
from .visual_selection import (
    ConversationSelectionActuator,
    ConversationSelectionOutcome,
    ConversationSelectionStatus,
    DeepSeekVisualSelectionProvider,
    ScreenRect,
    VisualRowDecision,
    VisualRowFrame,
    VisualSelectionConfig,
)

__all__ = [
    "QQVMWorker",
    "QQVMDriverBridge",
    "MessageCursorStore",
    "QQVMWorkerProcess",
    "WindowsUIAQQAccessibility",
    "WorkerCommand",
    "WorkerKind",
    "WorkerResult",
    "WorkerStatus",
    "SelectionHandoff",
    "mint_selection_handoff",
    "ConversationSelectionActuator",
    "ConversationSelectionOutcome",
    "ConversationSelectionStatus",
    "DeepSeekVisualSelectionProvider",
    "ScreenRect",
    "VisualRowDecision",
    "VisualRowFrame",
    "VisualSelectionConfig",
]
