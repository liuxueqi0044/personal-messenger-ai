"""Durable navigation work and retry budgets, isolated from message/send journals.

No screenshot, chat text, credential, or active-chat lease is persisted here.
Reservations happen before provider/UI calls, so a crash cannot refund a budget.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator
from uuid import uuid4

from messenger_ai.adapters.qq.navigation.contracts import ContactTarget


def _time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("navigation timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True, slots=True)
class NavigationTask:
    task_id: str
    target: ContactTarget
    pending_input_key: str
    status: str
    created_at: datetime
    updated_at: datetime
    retry_at: datetime | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class NavigationEpisode:
    episode_id: str
    task_id: str
    owner_id: str
    started_at: datetime
    deadline_at: datetime
    status: str
    model_requests: int
    desktop_actions: int


@dataclass(frozen=True, slots=True)
class NavigationEpisodeStart:
    episode: NavigationEpisode | None = None
    retry_at: datetime | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class ObservationRecoveryReceipt:
    """Consumed recovery quota and historical scope, never UI authority."""

    claim_id: str
    owner_id: str
    account_id: str
    conversation_id: str
    binding_id: str
    binding_revision: int
    conversation_revision: int
    global_revision: int
    pause_reason: str
    failure_episode_id: str
    claimed_at: datetime


_OBSERVATION_RECOVERY_REASON = "ui_automation_unavailable:identity_profile_capture_failed"


class NavigationTaskStore:
    """Compatible additional tables in the runtime SQLite database.

    A new store instance never resumes a running episode: it waits for its
    durable deadline. This avoids invalidating another live process's lease,
    while abandoned reservations remain charged across restarts.
    """

    def __init__(self, path: str | Path = ":memory:", *, owner_id: str | None = None):
        self.owner_id = owner_id or str(uuid4())
        self.connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self.connection.executescript("""
          CREATE TABLE IF NOT EXISTS runtime_nav_tasks(
            task_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL, binding_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL, target_json TEXT NOT NULL,
            pending_input_key TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            retry_at TEXT, error_code TEXT,
            UNIQUE(account_id,conversation_id,binding_id,binding_revision,pending_input_key)
          );
          CREATE TABLE IF NOT EXISTS runtime_nav_episodes(
            episode_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
            account_id TEXT NOT NULL, binding_id TEXT NOT NULL,
            owner_id TEXT NOT NULL, started_at TEXT NOT NULL, deadline_at TEXT NOT NULL,
            status TEXT NOT NULL, model_requests INTEGER NOT NULL DEFAULT 0,
            desktop_actions INTEGER NOT NULL DEFAULT 0, finished_at TEXT, error_code TEXT
          );
          CREATE INDEX IF NOT EXISTS runtime_nav_episode_scope
            ON runtime_nav_episodes(account_id,binding_id,started_at);
          CREATE TABLE IF NOT EXISTS runtime_nav_observation_recoveries(
            claim_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
            account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            binding_id TEXT NOT NULL, binding_revision INTEGER NOT NULL,
            conversation_revision INTEGER NOT NULL, global_revision INTEGER NOT NULL,
            pause_reason TEXT NOT NULL, failure_episode_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL, finished_at TEXT,
            status TEXT NOT NULL CHECK(status IN ('consumed','succeeded','failed')),
            UNIQUE(account_id,conversation_id,binding_id,binding_revision,pause_reason)
          );
        """)

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    @staticmethod
    def stable_task_id(target: ContactTarget, pending_input_key: str) -> str:
        if not pending_input_key:
            raise ValueError("pending input key is required")
        scope = [target.account_id, target.conversation_id, target.binding_id,
                 target.binding_revision, pending_input_key]
        digest = hashlib.sha256(json.dumps(scope, separators=(",", ":")).encode()).hexdigest()
        return f"qq-nav:{digest}"

    @staticmethod
    def _task(row: sqlite3.Row) -> NavigationTask:
        return NavigationTask(
            task_id=row["task_id"], target=ContactTarget.model_validate_json(row["target_json"]),
            pending_input_key=row["pending_input_key"], status=row["status"],
            created_at=_parse(row["created_at"]), updated_at=_parse(row["updated_at"]),
            retry_at=_parse(row["retry_at"]), error_code=row["error_code"],
        )

    @staticmethod
    def _episode(row: sqlite3.Row) -> NavigationEpisode:
        return NavigationEpisode(
            episode_id=row["episode_id"], task_id=row["task_id"], owner_id=row["owner_id"],
            started_at=_parse(row["started_at"]), deadline_at=_parse(row["deadline_at"]),
            status=row["status"], model_requests=row["model_requests"],
            desktop_actions=row["desktop_actions"],
        )

    def get_task(self, task_id: str) -> NavigationTask:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM runtime_nav_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task(row)

    def get_episode(self, episode_id: str) -> NavigationEpisode:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM runtime_nav_episodes WHERE episode_id=?", (episode_id,)
            ).fetchone()
        if row is None:
            raise KeyError(episode_id)
        return self._episode(row)

    def claim_observation_recovery(self, target: ContactTarget, *, conversation_revision: int,
                                   global_revision: int, pause_reason: str,
                                   now: datetime) -> ObservationRecoveryReceipt | None:
        """Consume one binding-revision recovery before any UI call.

        Conversation/control changes cannot mint a second chance. A crash or
        failed recovery leaves the quota consumed permanently; live runtime
        admission and UI/cleanup obligations belong to the caller.
        """
        if pause_reason != _OBSERVATION_RECOVERY_REASON:
            return None
        if (type(conversation_revision) is not int or conversation_revision < 0
                or type(global_revision) is not int or global_revision < 0):
            raise ValueError("observation recovery revisions must be nonnegative integers")
        now = _time(now)
        pending_key = f"observe:{target.conversation_id}:{conversation_revision}"
        scope = (target.account_id, target.conversation_id, target.binding_id,
                 target.binding_revision, pause_reason)
        with self._transaction():
            if self.connection.execute(
                "SELECT 1 FROM runtime_nav_observation_recoveries WHERE account_id=? "
                "AND conversation_id=? AND binding_id=? AND binding_revision=? AND pause_reason=?", scope,
            ).fetchone() is not None:
                return None
            failure = self.connection.execute(
                "SELECT e.episode_id,e.finished_at,t.target_json,t.retry_at FROM runtime_nav_episodes e "
                "JOIN runtime_nav_tasks t ON t.task_id=e.task_id "
                "WHERE t.account_id=? AND t.conversation_id=? AND t.binding_id=? AND t.binding_revision=? "
                "AND t.pending_input_key=? AND e.account_id=? AND e.binding_id=? "
                "AND e.error_code='identity_profile_capture_failed' AND e.status!='candidate_opened' "
                "AND e.finished_at IS NOT NULL AND e.finished_at!='' "
                "ORDER BY e.started_at ASC,e.episode_id ASC LIMIT 1",
                (target.account_id, target.conversation_id, target.binding_id, target.binding_revision,
                 pending_key, target.account_id, target.binding_id),
            ).fetchone()
            if failure is None:
                return None
            try:
                if (ContactTarget.model_validate_json(failure["target_json"]) != target
                        or _time(_parse(failure["finished_at"])) > now):
                    return None
                # Mirror begin_episode's existing fixed safe policy without
                # reserving an episode or rewriting old task/episode journals.
                # A cooling/rate-limited navigation must not burn this quota.
                retry_at = _parse(failure["retry_at"])
                if retry_at is not None and _time(retry_at) > now:
                    return None
                running = self.connection.execute(
                    "SELECT deadline_at FROM runtime_nav_episodes WHERE account_id=? AND binding_id=? "
                    "AND status='running'", (target.account_id, target.binding_id),
                ).fetchall()
                for episode in running:
                    deadline = _time(_parse(episode["deadline_at"]))
                    # begin_episode would mark an expired running reservation
                    # abandoned and apply the same 10s cooldown from deadline.
                    if deadline + timedelta(seconds=10) > now:
                        return None
                latest = self.connection.execute(
                    "SELECT finished_at FROM runtime_nav_episodes WHERE account_id=? AND binding_id=? "
                    "AND finished_at IS NOT NULL AND status!='candidate_opened' "
                    "ORDER BY finished_at DESC LIMIT 1", (target.account_id, target.binding_id),
                ).fetchone()
                if latest and _time(_parse(latest["finished_at"])) + timedelta(seconds=10) > now:
                    return None
            except (ValueError, TypeError, AttributeError):
                return None
            recent = self.connection.execute(
                "SELECT COUNT(*) FROM runtime_nav_episodes WHERE account_id=? AND binding_id=? "
                "AND started_at>? AND status!='candidate_opened'",
                (target.account_id, target.binding_id, (now - timedelta(seconds=300)).isoformat()),
            ).fetchone()[0]
            if recent >= 3:
                return None
            receipt = ObservationRecoveryReceipt(
                claim_id=str(uuid4()), owner_id=self.owner_id,
                account_id=target.account_id, conversation_id=target.conversation_id,
                binding_id=target.binding_id, binding_revision=target.binding_revision,
                conversation_revision=conversation_revision, global_revision=global_revision,
                pause_reason=pause_reason, failure_episode_id=failure["episode_id"], claimed_at=now,
            )
            self.connection.execute(
                "INSERT INTO runtime_nav_observation_recoveries(claim_id,owner_id,account_id,conversation_id,"
                "binding_id,binding_revision,conversation_revision,global_revision,pause_reason,failure_episode_id,"
                "claimed_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,'consumed')",
                (receipt.claim_id, receipt.owner_id, receipt.account_id, receipt.conversation_id,
                 receipt.binding_id, receipt.binding_revision, receipt.conversation_revision, receipt.global_revision,
                 receipt.pause_reason, receipt.failure_episode_id, receipt.claimed_at.isoformat()),
            )
        return receipt

    def finish_observation_recovery(self, receipt: ObservationRecoveryReceipt, *,
                                    succeeded: bool, now: datetime) -> bool:
        """Settle only this store owner's exact consumed receipt, with no refund."""
        if type(receipt) is not ObservationRecoveryReceipt or receipt.owner_id != self.owner_id:
            return False
        if type(succeeded) is not bool:
            raise ValueError("observation recovery settlement requires a boolean")
        now = _time(now)
        claimed_at = _time(receipt.claimed_at)
        if now < claimed_at:
            return False
        with self._transaction():
            changed = self.connection.execute(
                "UPDATE runtime_nav_observation_recoveries SET status=?,finished_at=? "
                "WHERE claim_id=? AND owner_id=? AND account_id=? AND conversation_id=? "
                "AND binding_id=? AND binding_revision=? AND conversation_revision=? AND global_revision=? "
                "AND pause_reason=? AND failure_episode_id=? AND claimed_at=? "
                "AND status='consumed' AND finished_at IS NULL",
                ("succeeded" if succeeded else "failed", now.isoformat(), receipt.claim_id, self.owner_id,
                 receipt.account_id, receipt.conversation_id, receipt.binding_id, receipt.binding_revision,
                 receipt.conversation_revision, receipt.global_revision, receipt.pause_reason,
                 receipt.failure_episode_id, claimed_at.isoformat()),
            ).rowcount
        return changed == 1

    def ensure_task(self, target: ContactTarget, pending_input_key: str, *,
                    now: datetime, task_id: str | None = None) -> NavigationTask:
        if not pending_input_key or len(pending_input_key) > 512:
            raise ValueError("pending input key must contain 1 to 512 characters")
        task_id = task_id or self.stable_task_id(target, pending_input_key)
        if not task_id or len(task_id) > 256:
            raise ValueError("task id must contain 1 to 256 characters")
        now = _time(now)
        with self._transaction():
            row = self.connection.execute(
                "SELECT * FROM runtime_nav_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if row is not None and (
                ContactTarget.model_validate_json(row["target_json"]) != target
                or row["pending_input_key"] != pending_input_key
            ):
                raise ValueError("navigation task id is already bound to different work")
            row = row or self.connection.execute(
                "SELECT * FROM runtime_nav_tasks WHERE account_id=? AND conversation_id=? "
                "AND binding_id=? AND binding_revision=? AND pending_input_key=?",
                (target.account_id, target.conversation_id, target.binding_id,
                 target.binding_revision, pending_input_key),
            ).fetchone()
            if row is not None:
                if ContactTarget.model_validate_json(row["target_json"]) != target:
                    raise ValueError("navigation target changed without a binding revision")
                return self._task(row)
            self.connection.execute(
                "INSERT INTO runtime_nav_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, target.account_id, target.conversation_id, target.binding_id,
                 target.binding_revision, target.model_dump_json(), pending_input_key,
                 "pending", now.isoformat(), now.isoformat(), None, None),
            )
        return self.get_task(task_id)

    def begin_episode(self, task_id: str, *, now: datetime,
                      total_timeout_seconds: float = 45,
                      cooldown_seconds: float = 10,
                      max_episodes: int = 3,
                      window_seconds: float = 300) -> NavigationEpisodeStart:
        if total_timeout_seconds <= 0 or cooldown_seconds < 10 or not 1 <= max_episodes <= 3 or window_seconds < 300:
            raise ValueError("navigation retry policy exceeds its safe bounds")
        now = _time(now)
        with self._transaction():
            task = self.get_task(task_id)
            scope = (task.target.account_id, task.target.binding_id)
            running = self.connection.execute(
                "SELECT * FROM runtime_nav_episodes WHERE account_id=? AND binding_id=? "
                "AND status='running'", scope,
            ).fetchall()
            for row in running:
                deadline = _parse(row["deadline_at"])
                if deadline > now:
                    return NavigationEpisodeStart(retry_at=deadline, error_code="navigation_episode_in_progress")
                self.connection.execute(
                    "UPDATE runtime_nav_episodes SET status='abandoned',finished_at=?,error_code=? "
                    "WHERE episode_id=?",
                    (deadline.isoformat(), "navigation_runtime_restarted", row["episode_id"]),
                )
                retry_at = deadline + timedelta(seconds=cooldown_seconds)
                self.connection.execute(
                    "UPDATE runtime_nav_tasks SET status='retry_wait',retry_at=?,error_code=?,updated_at=? "
                    "WHERE task_id=?",
                    (retry_at.isoformat(), "navigation_runtime_restarted", now.isoformat(), row["task_id"]),
                )
            # Contact-wide cooldown prevents a new pending input bypassing a failed episode.
            latest = self.connection.execute(
                "SELECT finished_at FROM runtime_nav_episodes WHERE account_id=? AND binding_id=? "
                "AND finished_at IS NOT NULL AND status!='candidate_opened' "
                "ORDER BY finished_at DESC LIMIT 1", scope,
            ).fetchone()
            retry_at = task.retry_at
            if latest:
                contact_retry = _parse(latest["finished_at"]) + timedelta(seconds=cooldown_seconds)
                retry_at = max(filter(None, (retry_at, contact_retry)))
            if retry_at is not None and retry_at > now:
                return NavigationEpisodeStart(retry_at=retry_at, error_code="navigation_cooldown")
            window_start = now - timedelta(seconds=window_seconds)
            recent = self.connection.execute(
                "SELECT started_at FROM runtime_nav_episodes WHERE account_id=? AND binding_id=? "
                "AND started_at>? AND status!='candidate_opened' ORDER BY started_at ASC",
                (*scope, window_start.isoformat()),
            ).fetchall()
            if len(recent) >= max_episodes:
                retry_at = _parse(recent[len(recent) - max_episodes]["started_at"]) + timedelta(seconds=window_seconds)
                return NavigationEpisodeStart(retry_at=retry_at, error_code="navigation_episode_rate_limited")
            episode_id = str(uuid4())
            deadline = now + timedelta(seconds=total_timeout_seconds)
            self.connection.execute(
                "INSERT INTO runtime_nav_episodes(episode_id,task_id,account_id,binding_id,owner_id,"
                "started_at,deadline_at,status) VALUES(?,?,?,?,?,?,?,'running')",
                (episode_id, task_id, *scope, self.owner_id, now.isoformat(), deadline.isoformat()),
            )
            self.connection.execute(
                "UPDATE runtime_nav_tasks SET status='running',retry_at=NULL,error_code=NULL,updated_at=? WHERE task_id=?",
                (now.isoformat(), task_id),
            )
        return NavigationEpisodeStart(episode=self.get_episode(episode_id))

    def _reserve(self, episode_id: str, column: str, maximum: int, now: datetime) -> bool:
        if column not in {"model_requests", "desktop_actions"} or maximum < 1:
            raise ValueError("invalid navigation reservation")
        with self._transaction():
            result = self.connection.execute(
                f"UPDATE runtime_nav_episodes SET {column}={column}+1 WHERE episode_id=? "
                f"AND owner_id=? AND status='running' AND deadline_at>? AND {column}<?",
                (episode_id, self.owner_id, _time(now).isoformat(), maximum),
            )
            return result.rowcount == 1

    def reserve_model_request(self, episode_id: str, *, now: datetime, maximum: int = 4) -> bool:
        return self._reserve(episode_id, "model_requests", maximum, now)

    def reserve_desktop_action(self, episode_id: str, *, now: datetime, maximum: int = 6) -> bool:
        return self._reserve(episode_id, "desktop_actions", maximum, now)

    def finish_episode(self, episode_id: str, *, status: str, now: datetime,
                       error_code: str | None = None, cooldown_seconds: float = 10) -> NavigationTask:
        if status not in {"candidate_opened", "retry_wait", "needs_attention", "cancelled"} or cooldown_seconds < 10:
            raise ValueError("invalid navigation settlement")
        now = _time(now)
        with self._transaction():
            episode = self.get_episode(episode_id)
            if episode.owner_id != self.owner_id or episode.status != "running":
                raise ValueError("navigation episode is no longer owned and running")
            retry_at = None if status == "candidate_opened" else now + timedelta(seconds=cooldown_seconds)
            self.connection.execute(
                "UPDATE runtime_nav_episodes SET status=?,finished_at=?,error_code=? WHERE episode_id=?",
                (status, now.isoformat(), error_code, episode_id),
            )
            self.connection.execute(
                "UPDATE runtime_nav_tasks SET status=?,updated_at=?,retry_at=?,error_code=? WHERE task_id=?",
                (status, now.isoformat(), retry_at.isoformat() if retry_at else None,
                 error_code, episode.task_id),
            )
        return self.get_task(episode.task_id)

    def record_already_current(self, task_id: str, *, now: datetime) -> NavigationTask:
        """Persist only a historical success; never persist or replay its lease."""
        with self._transaction():
            self.get_task(task_id)
            self.connection.execute(
                "UPDATE runtime_nav_tasks SET status='candidate_opened',updated_at=?,retry_at=NULL,error_code=NULL "
                "WHERE task_id=? AND status!='running'", (_time(now).isoformat(), task_id),
            )
        return self.get_task(task_id)
