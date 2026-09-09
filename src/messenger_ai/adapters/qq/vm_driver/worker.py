from __future__ import annotations

import multiprocessing as mp
import threading
from datetime import UTC, datetime
from uuid import UUID, uuid4

from messenger_ai.adapters.qq.models import QQIdentityBinding, QQSelectorPack
from messenger_ai.adapters.qq.ports import QQAccessibilityPort

from .contracts import WorkerCommand, WorkerKind, WorkerResult, WorkerStatus
from .transport import WindowsUIAQQAccessibility


class QQVMWorker:
    """One serial UIA execution lane. Instantiate this class in the VM process."""

    def __init__(self, *, accessibility: QQAccessibilityPort, selector_pack: QQSelectorPack, bindings: tuple[QQIdentityBinding, ...]) -> None:
        self._accessibility = accessibility
        self._selectors = selector_pack
        self._bindings = {item.binding_id: item for item in bindings}
        if len(self._bindings) != len(bindings):
            raise ValueError("binding_id must be unique")
        self._epoch = uuid4()
        self._lock = threading.Lock()
        self._prepared: dict[UUID, dict[str, object]] = {}
        self._reservation: UUID | None = None
        self._committed: set[UUID] = set()

    def execute(self, command: WorkerCommand) -> WorkerResult:
        """Runs exactly one fixed UI action under the worker's only UI lock."""
        with self._lock:
            if command.deadline is not None and command.deadline <= datetime.now(UTC):
                return self._result(command, WorkerStatus.FAILED_SAFE, "deadline_expired")
            if command.requires_binding() and command.binding_id not in self._bindings:
                return self._result(command, WorkerStatus.FAILED_SAFE, "unknown_binding")
            if self._reservation is not None and command.operation_id != self._reservation:
                return self._result(command, WorkerStatus.FAILED_SAFE, "ui_reserved")
            try:
                if command.kind is WorkerKind.HEALTH:
                    return self._health(command)
                if command.kind is WorkerKind.OBSERVE:
                    return self._observe(command)
                if command.kind is WorkerKind.PREPARE:
                    return self._prepare(command)
                if command.kind is WorkerKind.COMMIT:
                    return self._commit(command)
                if command.kind is WorkerKind.VERIFY:
                    return self._verify(command)
                if command.kind is WorkerKind.ABORT:
                    return self._abort(command)
                if command.kind is WorkerKind.STOP:
                    return self._result(command, WorkerStatus.OK)
            except Exception as exc:  # an action after commit intent is outcome-unknown.
                if command.kind is WorkerKind.COMMIT:
                    return self._result(command, WorkerStatus.UNCERTAIN, type(exc).__name__)
                return self._result(command, WorkerStatus.FAILED_SAFE, type(exc).__name__)
            return self._result(command, WorkerStatus.FAILED_SAFE, "unsupported_command")

    def _health(self, command: WorkerCommand) -> WorkerResult:
        windows = self._accessibility.find_main_windows(self._selectors.selector("main_window"))
        if len(windows) != 1:
            return self._result(command, WorkerStatus.UNAVAILABLE, "qq_window_ambiguous")
        return self._result(command, WorkerStatus.OK, evidence={"process_id": windows[0].process_id, "window_handle": windows[0].window_handle})

    def _observe(self, command: WorkerCommand) -> WorkerResult:
        binding = self._bindings[command.binding_id or ""]
        window, conversation = self._resolve(binding)
        bubbles = self._accessibility.list_bubbles(window, self._selectors.selector("bubbles"))
        # The durable, conversation-scoped cursor is assigned by the main process.
        rows = [item.model_dump(mode="json") for item in bubbles]
        complete = all(item.direction.value != "unknown" for item in bubbles)
        return self._result(command, WorkerStatus.OK if complete else WorkerStatus.FAILED_SAFE, None if complete else "direction_unknown", evidence={"target": conversation.model_dump(mode="json"), "bubbles": rows, "complete": complete, "gap": not complete})

    def _prepare(self, command: WorkerCommand) -> WorkerResult:
        if command.operation_id is None or command.text is None or command.segment_ref is None:
            return self._result(command, WorkerStatus.FAILED_SAFE, "prepare_fields_missing")
        window, conversation = self._resolve(self._bindings[command.binding_id or ""])
        before = self._accessibility.list_bubbles(window, self._selectors.selector("bubbles"))
        if self._accessibility.read_composer(window, self._selectors.selector("composer")):
            return self._result(command, WorkerStatus.FAILED_SAFE, "composer_not_empty")
        self._accessibility.write_composer(window, command.text, self._selectors.selector("composer"))
        actual = self._accessibility.read_composer(window, self._selectors.selector("composer"))
        if actual != command.text:
            return self._result(command, WorkerStatus.FAILED_SAFE, "composer_readback_mismatch")
        evidence = {"conversation": conversation.model_dump(mode="json"), "before_bubbles": [item.model_dump(mode="json") for item in before], "text_hash": __import__("hashlib").sha256(command.text.encode()).hexdigest(), "composer_text": command.text, "segment_ref": command.segment_ref}
        self._prepared[command.operation_id] = evidence
        self._reservation = command.operation_id
        return self._result(command, WorkerStatus.OK, evidence=evidence)

    def _commit(self, command: WorkerCommand) -> WorkerResult:
        evidence = self._prepared.get(command.operation_id) if command.operation_id else None
        if evidence is None:
            return self._result(command, WorkerStatus.FAILED_SAFE, "not_prepared")
        if command.operation_id in self._committed:
            return self._result(command, WorkerStatus.UNCERTAIN, "commit_already_attempted")
        window, conversation = self._resolve(self._bindings[command.binding_id or ""])
        current = conversation.model_dump(mode="json")
        if current != evidence["conversation"]:
            return self._result(command, WorkerStatus.FAILED_SAFE, "target_drift")
        if self._accessibility.read_composer(window, self._selectors.selector("composer")) != evidence["composer_text"]:
            return self._result(command, WorkerStatus.FAILED_SAFE, "composer_drift")
        current_bubbles = [item.model_dump(mode="json") for item in self._accessibility.list_bubbles(window, self._selectors.selector("bubbles"))]
        if current_bubbles != evidence["before_bubbles"]:
            return self._result(command, WorkerStatus.FAILED_SAFE, "stale_context")
        self._committed.add(command.operation_id)
        self._accessibility.invoke_send(window, self._selectors.selector("send"))
        return self._result(command, WorkerStatus.OK, evidence=evidence)

    def _verify(self, command: WorkerCommand) -> WorkerResult:
        evidence = self._prepared.get(command.operation_id) if command.operation_id else None
        if evidence is None:
            return self._result(command, WorkerStatus.FAILED_SAFE, "not_prepared")
        window, conversation = self._resolve(self._bindings[command.binding_id or ""])
        before = evidence["before_bubbles"]  # type: ignore[index]
        text_hash = str(evidence["text_hash"])
        after = self._accessibility.list_bubbles(window, self._selectors.selector("bubbles"))
        new_rows = _new_suffix(before, [item.model_dump(mode="json") for item in after])
        if new_rows is None:
            self._reservation = None
            return self._result(command, WorkerStatus.UNCERTAIN, "message_anchor_gap")
        verified = [item for item in after[-len(new_rows):] if item.direction.value == "outbound" and item.text_hash == text_hash] if new_rows else []
        if len(verified) != 1:
            self._reservation = None
            return self._result(command, WorkerStatus.UNCERTAIN, "outbound_receipt_not_unique", evidence={"conversation": conversation.model_dump(mode="json"), "after_bubbles": [item.model_dump(mode="json") for item in after]})
        self._reservation = None
        return self._result(command, WorkerStatus.OK, evidence={"conversation": conversation.model_dump(mode="json"), "receipt": verified[0].model_dump(mode="json"), "operation_id": str(command.operation_id)})

    def _abort(self, command: WorkerCommand) -> WorkerResult:
        if command.operation_id is None or command.operation_id != self._reservation:
            return self._result(command, WorkerStatus.FAILED_SAFE, "abort_not_owner")
        if command.operation_id in self._committed:
            return self._result(command, WorkerStatus.UNCERTAIN, "committed_cannot_abort")
        evidence = self._prepared.get(command.operation_id)
        if evidence is None or command.binding_id not in self._bindings:
            return self._result(command, WorkerStatus.FAILED_SAFE, "abort_evidence_missing")
        window, conversation = self._resolve(self._bindings[command.binding_id])
        if conversation.model_dump(mode="json") != evidence["conversation"]:
            return self._result(command, WorkerStatus.FAILED_SAFE, "needs_manual_cleanup")
        current = self._accessibility.read_composer(window, self._selectors.selector("composer"))
        if current != evidence["composer_text"]:
            return self._result(command, WorkerStatus.FAILED_SAFE, "needs_manual_cleanup")
        self._accessibility.write_composer(window, "", self._selectors.selector("composer"))
        if self._accessibility.read_composer(window, self._selectors.selector("composer")) != "":
            return self._result(command, WorkerStatus.FAILED_SAFE, "needs_manual_cleanup")
        self._prepared.pop(command.operation_id, None)
        self._reservation = None
        return self._result(command, WorkerStatus.OK)

    def _resolve(self, binding: QQIdentityBinding):
        windows = self._accessibility.find_main_windows(self._selectors.selector("main_window"))
        if len(windows) != 1:
            raise RuntimeError("QQ window absent or ambiguous")
        window = windows[0]
        matches = [item for item in self._accessibility.list_conversations(window, self._selectors.selector("conversations")) if binding.matches(item)]
        if len(matches) != 1:
            raise RuntimeError("binding target absent or ambiguous")
        conversation = matches[0]
        self._accessibility.select_conversation(window, conversation, self._selectors.selector("conversation_item"))
        # Re-query after select: a title/name alone is never treated as identity.
        confirmed = [item for item in self._accessibility.list_conversations(window, self._selectors.selector("conversations")) if binding.matches(item)]
        if len(confirmed) != 1:
            raise RuntimeError("binding proof drift after selection")
        return window, conversation

    def _result(self, command: WorkerCommand, status: WorkerStatus, error_code: str | None = None, evidence: dict[str, object] | None = None) -> WorkerResult:
        return WorkerResult(request_id=command.request_id, kind=command.kind, status=status, worker_epoch=self._epoch, operation_id=command.operation_id, binding_id=command.binding_id, binding_revision=command.binding_revision, conversation_revision=command.conversation_revision, error_code=error_code, evidence=evidence or {})


class QQVMWorkerProcess:
    """Spawn-only guest process façade; host callers never touch QQ UIA directly."""

    def __init__(self, selector_pack: QQSelectorPack, bindings: tuple[QQIdentityBinding, ...]) -> None:
        self._selector_pack, self._bindings = selector_pack, bindings
        self._parent, child = mp.get_context("spawn").Pipe()
        self._process = mp.get_context("spawn").Process(target=_serve, args=(child, selector_pack, bindings), daemon=True)

    def start(self) -> None:
        self._process.start()

    def request(self, command: WorkerCommand, timeout_seconds: float) -> WorkerResult:
        if not self._process.is_alive():
            return WorkerResult(request_id=command.request_id, kind=command.kind, status=WorkerStatus.UNAVAILABLE, worker_epoch=UUID(int=0), error_code="worker_not_alive")
        self._parent.send(command.model_dump(mode="json"))
        if not self._parent.poll(timeout_seconds):
            self._process.terminate()
            self._process.join(5)
            return WorkerResult(request_id=command.request_id, kind=command.kind, status=WorkerStatus.UNCERTAIN, worker_epoch=UUID(int=0), operation_id=command.operation_id, error_code="worker_timeout_isolated")
        result = WorkerResult.model_validate(self._parent.recv())
        if result.request_id != command.request_id or result.operation_id != command.operation_id:
            self._process.terminate()
            self._process.join(5)
            return WorkerResult(request_id=command.request_id, kind=command.kind, status=WorkerStatus.UNCERTAIN, worker_epoch=UUID(int=0), operation_id=command.operation_id, error_code="worker_response_mismatch")
        return result

    def stop(self, timeout_seconds: float = 5) -> None:
        if self._process.is_alive():
            self._parent.send(WorkerCommand(kind=WorkerKind.STOP).model_dump(mode="json"))
            self._process.join(timeout_seconds)


def _serve(connection, selector_pack: QQSelectorPack, bindings: tuple[QQIdentityBinding, ...]) -> None:
    worker = QQVMWorker(accessibility=WindowsUIAQQAccessibility(), selector_pack=selector_pack, bindings=bindings)
    while True:
        command = WorkerCommand.model_validate(connection.recv())
        result = worker.execute(command)
        connection.send(result.model_dump(mode="json"))
        if command.kind is WorkerKind.STOP:
            return


def _new_suffix(before: list[object], after: list[dict[str, object]]) -> list[dict[str, object]] | None:
    """Find only an ordered post-snapshot tail; never subtract unstable UI keys."""
    expected = [(row.get("direction"), row.get("text")) for row in before if isinstance(row, dict)]
    actual = [(row.get("direction"), row.get("text")) for row in after]
    for overlap in range(min(len(expected), len(actual)), 0, -1):
        if expected[len(expected) - overlap :] == actual[:overlap]:
            return after[overlap:]
    return None
