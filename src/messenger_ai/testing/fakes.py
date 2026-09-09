from collections import defaultdict
from datetime import UTC, datetime, timedelta

from ..domain.models import *
from ..domain.state_machines import SendStateMachine


class FakeClock:
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2025, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float):
        self._now += timedelta(seconds=seconds)
        return self._now

    def set(self, value: datetime):
        self._now = value


class FakeAdapter:
    def __init__(self):
        self.inbound = []
        self.operations = []
        self.submitted_keys = set()
        self.uncertain = False

    async def poll_events(self, cursor=None):
        return list(self.inbound)

    async def prepare_send(self, command):
        op = SendOperation(
            idempotency_key=command.idempotency_key, draft_id=command.draft_id
        )
        SendStateMachine.transition(op, SendStatus.PREPARED)
        return op

    async def commit_send(self, operation):
        if operation.idempotency_key in self.submitted_keys:
            return operation
        self.submitted_keys.add(operation.idempotency_key)
        self.operations.append(operation)
        if self.uncertain:
            SendStateMachine.transition(
                operation, SendStatus.UNCERTAIN, "SEND_UNCERTAIN"
            )
        else:
            SendStateMachine.transition(operation, SendStatus.COMMITTED)
        return operation

    async def verify_send(self, operation):
        if operation.status == SendStatus.UNCERTAIN:
            return operation
        return SendStateMachine.transition(operation, SendStatus.VERIFIED)


class FakeModel:
    def __init__(self, plan: ReplyPlan | None = None):
        self.plan_value = plan or ReplyPlan(reply_text="ok")
        self.calls = 0

    async def plan(self, messages):
        self.calls += 1
        return self.plan_value


class InMemoryStore:
    def __init__(self):
        self.events = {}
        self.idempotency = {}
        self.data = defaultdict(dict)

    def append_event(self, event, consumer: str = "default"):
        key = (str(event.event_id), consumer)
        if key in self.events:
            return False
        self.events[key] = event
        return True

    def claim_idempotency(self, key, value=True):
        if key in self.idempotency:
            return False
        self.idempotency[key] = value
        return True
