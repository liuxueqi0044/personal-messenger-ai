"""Small, real-port harness for future V5 daemon acceptance tests.

The harness keeps the VM worker and durable bridge in the loop.  It only
provides deterministic test controls around them; it does not emulate a send
target or short-circuit the bridge.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from messenger_ai.adapters.qq import BubbleDirection, QQBubble, QQConversation, QQIdentityBinding
from messenger_ai.domain import Platform
from messenger_ai.adapters.qq.vm_driver import QQVMDriverBridge, QQVMWorker
from messenger_ai.llm import ContactProjection, InboundItem, ReplyPlanRequest, RuleProjection, build_projection
from messenger_ai.memory import Contact, IdentityBinding, MemoryMessage, MemoryMessageDirection, MemoryService, SQLiteMemoryStore
from messenger_ai.testing import FakeClock


class LocalWorkerPort:
    """Synchronous worker port with an auditable request stream."""

    def __init__(self, worker: QQVMWorker) -> None:
        self.worker = worker
        self.requests: list[Any] = []

    def request(self, command: Any, _timeout: float) -> Any:
        self.requests.append(command)
        return self.worker.execute(command)


class DeferredModel:
    """Model stub whose results can be completed in an explicitly chosen order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._pending: dict[str, list[asyncio.Future[Any]]] = {}

    async def plan(self, contact_id: str, *messages: Any) -> Any:
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.calls.append((contact_id, messages))
        self._pending.setdefault(contact_id, []).append(future)
        return await future

    def resolve_next(self, contact_id: str, result: Any) -> None:
        queue = self._pending.get(contact_id, [])
        if not queue:
            raise AssertionError(f"no pending model call for {contact_id}")
        future = queue.pop(0)
        if not future.done():
            future.set_result(result)

    def pending(self, contact_id: str | None = None) -> int:
        if contact_id is not None:
            return len(self._pending.get(contact_id, []))
        return sum(len(items) for items in self._pending.values())


class V5VMHarness:
    """Three-to-five contact VM/bridge fixture with deterministic observations."""

    def __init__(self, tmp_path: str | Path, *, contacts: int = 3) -> None:
        if contacts not in (3, 5):
            raise ValueError("contacts must be 3 or 5")
        # Import the existing accessibility fixture rather than creating a
        # second fake UI implementation.  The worker and bridge remain real.
        adapter_path = Path(__file__).resolve().parents[1] / "adapters" / "qq" / "test_qq_adapter.py"
        spec = importlib.util.spec_from_file_location("v5_fixture_qq_adapter", adapter_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"cannot load QQ fixture adapter: {adapter_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _adapter = module._adapter

        _adapter_instance, self.accessibility = _adapter()
        self.clock = FakeClock(datetime(2026, 9, 9, 12, 0, tzinfo=UTC))
        self.bindings = tuple(
            QQIdentityBinding(
                hub_conversation_id=f"hub-{index}",
                contact_id=f"contact-{index}",
                account_id="account",
                platform_conversation_id=f"qq-{index}",
                participant_signature=f"proof-{index}",
                binding_id=f"binding-{index}",
            )
            for index in range(contacts)
        )
        self.accessibility.conversations = [
            QQConversation(
                internal_id=binding.platform_conversation_id,
                display_name=f"contact {index}",
                participant_signature=binding.participant_signature,
                last_message_key=f"anchor-{index}",
                tree_digest=self.accessibility.digest,
            )
            for index, binding in enumerate(self.bindings)
        ]
        self._selected = self.bindings[0].platform_conversation_id
        self._histories = {binding.platform_conversation_id: [] for binding in self.bindings}
        self._composers = {binding.platform_conversation_id: "" for binding in self.bindings}
        def select(_window, conversation, _selector): self._selected = conversation.internal_id
        def bubbles(_window, _selector): return list(self._histories[self._selected])
        def write(_window, text, _selector): self._composers[self._selected] = text
        def read(_window, _selector): return self._composers[self._selected]
        def invoke(_window, _selector):
            history=self._histories[self._selected]
            history.append(QQBubble(conversation_internal_id=self._selected,message_key=f"bot-{len(history)}",direction=BubbleDirection.OUTBOUND,text=self._composers[self._selected],observed_at=self.clock.now(),tree_digest=self.accessibility.digest))
            self._composers[self._selected]=""
        self.accessibility.select_conversation=select; self.accessibility.list_bubbles=bubbles
        self.accessibility.write_composer=write; self.accessibility.read_composer=read; self.accessibility.invoke_send=invoke
        self.worker = QQVMWorker(
            accessibility=self.accessibility,
            selector_pack=_adapter_instance.selector_pack,
            bindings=self.bindings,
        )
        self.port = LocalWorkerPort(self.worker)
        self.send_texts: dict[str, str] = {}
        self.bridge = QQVMDriverBridge(
            worker=self.port,
            bindings=self.bindings,
            text_provider=lambda command: self.send_texts.get(command.text_hash, ""),
            sqlite_path=Path(tmp_path) / "qq-vm.sqlite3",
        )
        self.model = DeferredModel()
        self.memory_store = SQLiteMemoryStore(Path(tmp_path) / "memory.sqlite3")
        self.memory = MemoryService(self.memory_store, clock=self.clock)
        for binding in self.bindings:
            self.memory.create_contact(Contact(contact_id=binding.contact_id, created_at=self.clock.now()))
            self.memory.bind_identity(IdentityBinding(
                contact_id=binding.contact_id, platform=Platform.QQ,
                account_id=binding.account_id, conversation_id=binding.hub_conversation_id,
                platform_evidence_hash=hashlib.sha256(binding.participant_signature.encode()).hexdigest(),
                verified_by="fixture", verified_at=self.clock.now()))

    def register_send_text(self, text: str) -> str:
        """Register the exact immutable text a real command is expected to send."""
        digest = hashlib.sha256(text.encode()).hexdigest()
        self.send_texts[digest] = text
        return digest

    def record_memory(self, index: int, text: str, *, direction: MemoryMessageDirection, key: str | None = None) -> MemoryMessage:
        binding = self.bindings[index]
        item = MemoryMessage(contact_id=binding.contact_id, conversation_id=binding.hub_conversation_id,
                             source_event_id=uuid4(),
                             platform_message_key=key or f"memory-{index}-{len(text)}", text=text,
                             observed_at=self.clock.now(), expires_at=self.clock.now() + timedelta(days=1),
                             direction=direction)
        return self.memory.record_message(item)

    def prompt_request(self, index: int, inbound: tuple[InboundItem, ...]) -> ReplyPlanRequest:
        binding = self.bindings[index]
        context = self.memory.context(binding.contact_id, binding.hub_conversation_id, budget_chars=12000)
        request = ReplyPlanRequest(request_id=f"fixture-{index}-{len(inbound)}", account_id=binding.account_id,
            contact=ContactProjection(contact_id=binding.contact_id, conversation_id=binding.hub_conversation_id,
                                      recent_messages=tuple(InboundItem(message_key=item.platform_message_key, text=item.text,
                                                                          observed_at=item.observed_at, direction=item.direction.value)
                                                            for item in context.recent_messages)),
            rules=RuleProjection(rulepack_id="fixture-rules", rule_version="fixture-rules-v1", source_hash="fixture-source",
                                 behavior=("answer naturally",), prohibited=("do not invent facts",)),
            inbound=inbound, context_fingerprint=f"fixture-context-{index}", created_at=self.clock.now())
        # Force prompt serialization here so tests exercise the production
        # direction-preserving projection rather than only model validation.
        build_projection(request)
        return request

    def append_inbound(self, index: int, text: str, *, key: str | None = None) -> str:
        return self._append(index, BubbleDirection.INBOUND, text, key=key)

    def append_human_outbound(self, index: int, text: str, *, key: str | None = None) -> str:
        return self._append(index, BubbleDirection.OUTBOUND, text, key=key)

    def _append(self, index: int, direction: BubbleDirection, text: str, *, key: str | None) -> str:
        binding = self.bindings[index]
        history=self._histories[binding.platform_conversation_id]
        message_key = key or f"{direction.value}-{len(history)}"
        history.append(
            QQBubble(
                conversation_internal_id=binding.platform_conversation_id,
                message_key=message_key,
                direction=direction,
                text=text,
                observed_at=self.clock.now(),
                tree_digest=self.accessibility.digest,
            )
        )
        return message_key

    async def observe(self, index: int, *, binding_revision: int = 1, conversation_revision: int = 1):
        return await self.bridge.observe_conversation(
            self.bindings[index].hub_conversation_id,
            binding_revision=binding_revision,
            conversation_revision=conversation_revision,
        )
