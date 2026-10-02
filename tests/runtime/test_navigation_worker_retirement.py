"""Real coordinator/operator/backend/process; only IPC and native values are fake."""
import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest

from messenger_ai.adapters.qq.navigation.contracts import NavigationDecision, NavigationProviderResult
from messenger_ai.adapters.qq.navigation.desktop import NavigationDesktopScope
from messenger_ai.adapters.qq.navigation.identity import ActiveChatLease, ActiveChatVerificationResult
from messenger_ai.adapters.qq.navigation.windows_backend import (
    ProcessScopedDesktopOperator, WindowsNavigationBackend, WindowsNavigationConfig,
)
from messenger_ai.adapters.qq.navigation.worker_process import NavigationWorkerProcess, NavigationWorkerResult
from messenger_ai.runtime.navigation import NavigationCoordinator
from messenger_ai.runtime.navigation_state import NavigationTaskStore
from tests.adapters.qq.navigation.test_contracts import request, click
from tests.adapters.qq.navigation.test_worker_process import Context, Pipe
from tests.adapters.qq.navigation.test_verified_click import GenericBackend


class ValuePipe(Pipe):
    def __init__(self, req, *, failure=None, region_changed=False):
        super().__init__()
        self.req, self.frame = req, req.frame
        self.failure, self.region_changed = failure, region_changed
        self.captures = 0
        self.failure_seen = False

    def send(self, command):
        self.sent.append(command)
        action = command["action"]
        if action == "capture":
            self.captures += 1
        fail = (
            self.failure == "roi_timeout" and action == "digest" and command["current"]
            or self.failure == "click_timeout" and action in {"click", "verified_click"}
            or self.failure == "postclick_timeout" and action == "capture" and self.captures > 1
            or self.failure == "worker_exited" and action == "digest" and command["current"]
        )
        if fail:
            self.failure_seen = True
            if self.failure != "worker_exited":
                return  # Actual NavigationWorkerProcess timeout revokes/reaps.
            values = {"error_code": "navigation_worker_exited"}
        elif action == "capture":
            self.frame = self.frame.model_copy(update={
                "frame_id": f"{self.frame.worker_epoch}-frame-{self.captures}",
                "captured_at": datetime.now(UTC),
            })
            values = {"frame": self.frame}
        elif action == "scope":
            fields = (
                "run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
                "control_revision", "binding_id", "binding_revision", "process_id", "window_handle",
                "screen_origin_x", "screen_origin_y", "screen_width", "screen_height",
                "crop_origin_x", "crop_origin_y", "crop_width", "crop_height", "dpi_scale",
            )
            scope = NavigationDesktopScope(
                **{name: getattr(self.frame, name) for name in fields},
                account_id=self.req.target.account_id, conversation_id=self.req.target.conversation_id,
                lease_expires_at=self.req.deadline_at, observed_at=datetime.now(UTC), foreground=True,
                paused=False, has_owned_draft=False, has_commit_obligation=False,
            )
            values = {"scope": scope}
        elif action == "digest":
            values = {"digest": ("b" if self.region_changed and command["current"] else "a") * 64}
        else:
            values = {"completed": True}
        self.inbox.append(NavigationWorkerResult(request_id=command["request_id"], **values).model_dump())


class ValueContext(Context):
    def __init__(self, req, **options):
        self.pipe = ValuePipe(req, **options)


class TrackedBackend(WindowsNavigationBackend):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.capture_attempts = 0

    async def bound_capture(self, target, *, deadline_at):
        self.capture_attempts += 1
        return await super().bound_capture(target, deadline_at=deadline_at)


class Navigator:
    def __init__(self):
        self.frames = []

    async def decide(self, req, *, cancel_event=None):
        self.frames.append(req.frame)
        decision = (click(frame_id=req.frame.frame_id) if len(self.frames) == 1
                    else NavigationDecision(action="candidate_opened", frame_id=req.frame.frame_id))
        return NavigationProviderResult(frame_id=req.frame.frame_id, decision=decision, model="synthetic", latency_ms=0)


class Verifier:
    def __init__(self, clock):
        self.clock, self.frames = clock, []

    async def verify(self, target, frame, *, deadline_at):
        self.frames.append(frame)
        if len(self.frames) == 1:
            return ActiveChatVerificationResult(verified=False, error_code="active_chat_not_current")
        now, tick = self.clock(), time.monotonic_ns()
        fields = {name: getattr(frame, name) for name in (
            "run_id", "session_epoch", "surface_epoch", "worker_epoch", "desktop_lease_id",
            "control_revision", "process_id", "window_handle", "frame_id",
        )}
        return ActiveChatVerificationResult(verified=True, lease=ActiveChatLease(
            **fields, lease_id="a"*64, account_id=target.account_id, conversation_id=target.conversation_id,
            binding_id=target.binding_id, binding_revision=target.binding_revision,
            observation_epoch="synthetic-observation", process_started_at_100ns=1,
            evidence_ref="synthetic-independent-evidence", evidence_digest="b"*64,
            verification_method="profile_from_current_header_with_selected_row_fence",
            issued_at=now, expires_at=now+timedelta(seconds=10),
            issued_monotonic_ns=tick, expires_monotonic_ns=tick+10_000_000_000,
        ))


def episode(req, store, *, failure=None, region_changed=False, clock=None, lock=None):
    context = ValueContext(req, failure=failure, region_changed=region_changed)
    worker = NavigationWorkerProcess(None, {}, context=context, deadline_at=req.deadline_at,
                                     max_seconds=1.25 if failure else 5)
    selectors = {f"{name}_selector": {"name": name, "control_type": "Text"}
                 for name in ("list", "row", "name", "search", "header", "composer", "message")}
    config = WindowsNavigationConfig(
        guard_state_path="C:/synthetic-not-read/guard.json", window={
            "process_id": req.frame.process_id, "window_handle": req.frame.window_handle, "class_name": "QQ"},
        expected_process_started_at_100ns=1, expected_run_id=req.frame.run_id,
        expected_worker_epoch=req.frame.worker_epoch, **selectors,
    )
    backend = TrackedBackend(config, req.target, worker=worker, deadline_at=req.deadline_at)
    # ROI/digest retirement exercises the preserved generic producer contract.
    # Production Windows clicks use the dedicated command, including timeout
    # and post-input failure tests below.
    generic = failure in {"roi_timeout", "worker_exited"} or region_changed
    operator = ProcessScopedDesktopOperator(backend=GenericBackend(backend) if generic else backend, operation_lock=lock)
    clock = clock or (lambda: datetime.now(UTC))
    navigator, verifier = Navigator(), Verifier(clock)
    coordinator = NavigationCoordinator(navigator, operator, verifier, store, clock=clock)
    return coordinator, backend, context, navigator, verifier


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,code,capture_attempts", [
    ("roi_timeout", "navigation_deadline_expired", 1),
    ("click_timeout", "navigation_local_result_unavailable", 1),
    ("postclick_timeout", "navigation_local_result_unavailable", 2),
    ("worker_exited", "navigation_worker_exited", 1),
])
async def test_retired_worker_finishes_episode_with_original_cause_without_recapture_or_click_replay(failure, code, capture_attempts):
    store = NavigationTaskStore()
    instance, backend, context, navigator, verifier = episode(request(), store, failure=failure)
    try:
        result = await asyncio.wait_for(instance.navigate(backend.target, pending_input_key="same-pending"), timeout=3)
        assert context.pipe.failure_seen
        assert result.outcome.status == "retry_wait" and result.outcome.error_code == code
        assert result.active_chat_lease is None
        assert result.outcome.model_requests == result.outcome.desktop_actions == 1
        assert len(navigator.frames) == len(verifier.frames) == 1
        assert backend.capture_attempts == capture_attempts
        assert len([x for x in context.pipe.sent if x["action"] == "verified_click"]) == (failure in {"click_timeout", "postclick_timeout"})
        assert not any(x["action"] == "click" for x in context.pipe.sent)
        assert backend.worker._closed and backend.worker.revoked.is_set() and not context.process.alive
        assert not instance.desktop.operation_lock.locked()
        row = store.connection.execute("SELECT status,error_code,deadline_at,started_at FROM runtime_nav_episodes").fetchone()
        assert row["status"] == "retry_wait" and row["error_code"] == code
        assert datetime.fromisoformat(row["deadline_at"])-datetime.fromisoformat(row["started_at"]) == timedelta(seconds=45)
    finally:
        backend.close()
        store.close()


@pytest.mark.asyncio
async def test_live_region_stale_still_uses_fresh_frame_model_and_independent_verifier():
    store = NavigationTaskStore()
    instance, backend, context, navigator, verifier = episode(request(), store, region_changed=True)
    try:
        result = await instance.navigate(backend.target, pending_input_key="region-change")
        assert result.outcome.status == "candidate_opened" and result.active_chat_lease is not None
        assert backend.capture_attempts == 2 and len(navigator.frames) == len(verifier.frames) == 2
        assert navigator.frames[0].frame_id != navigator.frames[1].frame_id
        assert not any(x["action"] in {"click", "verified_click"} for x in context.pipe.sent)
        assert not instance.desktop.operation_lock.locked()
    finally:
        backend.close()
        store.close()


@pytest.mark.asyncio
async def test_explicit_episode_after_cooldown_uses_successor_worker_and_new_decision():
    store, lock = NavigationTaskStore(), asyncio.Lock()
    offset = [0]
    clock = lambda: datetime.now(UTC)+timedelta(seconds=offset[0])
    req = request()
    old, backend, context, navigator, _ = episode(req, store, failure="click_timeout", clock=clock, lock=lock)
    successor_backend = None
    try:
        first = await old.navigate(req.target, pending_input_key="same-pending")
        old_command_count = len(context.pipe.sent)
        assert first.outcome.error_code == "navigation_local_result_unavailable"
        blocked = await old.navigate(req.target, pending_input_key="same-pending")
        assert blocked.outcome.error_code == "navigation_cooldown"
        assert len(context.pipe.sent) == old_command_count and len(navigator.frames) == 1
        offset[0] = 11
        successor_req = req.model_copy(update={"frame": req.frame.model_copy(update={"worker_epoch": "successor-worker"})})
        new, successor_backend, new_context, new_navigator, _ = episode(successor_req, store, clock=clock, lock=lock)
        second = await new.navigate(req.target, pending_input_key="same-pending")
        assert second.task_id == first.task_id and second.outcome.status == "candidate_opened"
        assert second.active_chat_lease.worker_epoch == "successor-worker"
        assert new_navigator.frames[0].frame_id != navigator.frames[0].frame_id
        assert len(context.pipe.sent) == old_command_count  # Retired actor is never reused.
        assert len([x for x in new_context.pipe.sent if x["action"] == "verified_click"]) == 1
        assert store.connection.execute("SELECT COUNT(*) FROM runtime_nav_episodes").fetchone()[0] == 2
        assert not lock.locked()
    finally:
        backend.close()
        if successor_backend is not None:
            successor_backend.close()
        store.close()
