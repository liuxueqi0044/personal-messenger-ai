from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import sqlite3
import threading
from uuid import UUID

import pytest

from messenger_ai.adapters.qq.navigation.contracts import ContactTarget
from messenger_ai.runtime.navigation_state import NavigationTaskStore


NOW = datetime(2026, 10, 2, tzinfo=UTC)
CLAIM_NOW = NOW + timedelta(seconds=10)
RECOVERY_REASON = "ui_automation_unavailable:identity_profile_capture_failed"


def target(**changes):
    return ContactTarget(account_id="test-account", conversation_id="test-conversation",
                         binding_id="test-binding", binding_revision=2,
                         display_name="Test contact", search_aliases=(), identity_mode="persistent").model_copy(update=changes)


def test_additional_tables_do_not_modify_message_business_history(tmp_path):
    path = tmp_path / "runtime.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE runtime_observations(local_message_key TEXT PRIMARY KEY, text TEXT)")
    db.execute("INSERT INTO runtime_observations VALUES('consumed-key','existing observation')")
    db.commit()
    store = NavigationTaskStore(path)
    task = store.ensure_task(target(), "pending-key", now=NOW)
    assert task.status == "pending"
    assert db.execute("SELECT * FROM runtime_observations").fetchall() == [("consumed-key", "existing observation")]
    assert "lease" not in " ".join(row[1] for row in db.execute("PRAGMA table_info(runtime_nav_tasks)"))
    store.close()
    db.close()


def test_pending_work_dedupes_and_rejects_rebound_task_or_unrevisioned_target():
    store = NavigationTaskStore()
    first = store.ensure_task(target(), "pending-key", task_id="stable-work", now=NOW)
    assert store.ensure_task(target(), "pending-key", task_id="another-id", now=NOW).task_id == first.task_id
    with pytest.raises(ValueError, match="different work"):
        store.ensure_task(target(), "different-key", task_id=first.task_id, now=NOW)
    with pytest.raises(ValueError, match="binding revision"):
        store.ensure_task(target(display_name="Changed name"), "pending-key", now=NOW)
    different = store.ensure_task(target(binding_revision=3), "pending-key", now=NOW)
    assert different.task_id != first.task_id


def test_restart_preserves_budget_and_never_resumes_running_lease(tmp_path):
    path = tmp_path / "runtime.db"
    original = NavigationTaskStore(path, owner_id="old-process")
    task = original.ensure_task(target(), "pending-key", now=NOW)
    episode = original.begin_episode(task.task_id, now=NOW).episode
    for _ in range(4):
        assert original.reserve_model_request(episode.episode_id, now=NOW)
    assert not original.reserve_model_request(episode.episode_id, now=NOW)
    original.close()
    restarted = NavigationTaskStore(path, owner_id="new-process")
    assert restarted.get_episode(episode.episode_id).model_requests == 4
    assert not restarted.reserve_model_request(episode.episode_id, now=NOW)
    blocked = restarted.begin_episode(task.task_id, now=NOW + timedelta(seconds=1))
    assert blocked.error_code == "navigation_episode_in_progress"
    assert blocked.retry_at == NOW + timedelta(seconds=45)
    assert restarted.begin_episode(task.task_id, now=NOW + timedelta(seconds=45)).error_code == "navigation_cooldown"
    assert restarted.get_episode(episode.episode_id).status == "abandoned"
    fresh = restarted.begin_episode(task.task_id, now=NOW + timedelta(seconds=55)).episode
    assert fresh.episode_id != episode.episode_id
    assert fresh.model_requests == 0
    assert restarted.get_task(task.task_id).pending_input_key == "pending-key"


def test_contact_wide_failure_window_and_cooldown_survive_new_pending_work_and_restart(tmp_path):
    path = tmp_path / "runtime.db"
    store = NavigationTaskStore(path)
    first = store.ensure_task(target(), "key-0", now=NOW)
    for index in range(3):
        now = NOW + timedelta(seconds=10 * index)
        task = store.ensure_task(target(), f"key-{index}", now=now)
        episode = store.begin_episode(task.task_id, now=now).episode
        assert episode is not None
        store.finish_episode(episode.episode_id, status="retry_wait", error_code="test_failure", now=now)
    store.close()
    store = NavigationTaskStore(path)
    fourth = store.ensure_task(target(), "key-3", now=NOW + timedelta(seconds=21))
    assert store.begin_episode(fourth.task_id, now=NOW + timedelta(seconds=21)).error_code == "navigation_cooldown"
    blocked = store.begin_episode(fourth.task_id, now=NOW + timedelta(seconds=30))
    assert blocked.error_code == "navigation_episode_rate_limited"
    assert blocked.retry_at == NOW + timedelta(seconds=300)
    assert store.begin_episode(first.task_id, now=NOW + timedelta(seconds=300)).episode is not None


def test_successful_normal_poll_does_not_consume_failure_window_or_cooldown():
    store = NavigationTaskStore()
    task = store.ensure_task(target(), "pending-key", now=NOW)
    for _ in range(5):
        episode = store.begin_episode(task.task_id, now=NOW).episode
        assert episode is not None
        store.finish_episode(episode.episode_id, status="candidate_opened", now=NOW)
    assert store.get_task(task.task_id).status == "candidate_opened"
    columns = {row[1] for row in store.connection.execute("PRAGMA table_info(runtime_nav_episodes)")}
    assert "active_chat_lease" not in columns


def test_atomic_reservations_and_settlement_cannot_be_reused_by_other_owner(tmp_path):
    path = tmp_path / "runtime.db"
    owner = NavigationTaskStore(path)
    task = owner.ensure_task(target(), "pending-key", now=NOW)
    episode = owner.begin_episode(task.task_id, now=NOW).episode
    other = NavigationTaskStore(path)
    assert not other.reserve_desktop_action(episode.episode_id, now=NOW)
    with pytest.raises(ValueError, match="owned"):
        other.finish_episode(episode.episode_id, status="candidate_opened", now=NOW)
    assert owner.reserve_desktop_action(episode.episode_id, now=NOW, maximum=1)
    assert not owner.reserve_desktop_action(episode.episode_id, now=NOW, maximum=1)
    owner.finish_episode(episode.episode_id, status="cancelled", now=NOW)
    assert not owner.reserve_model_request(episode.episode_id, now=NOW)


@pytest.mark.parametrize("policy", [{"cooldown_seconds": 9}, {"max_episodes": 4}, {"window_seconds": 299}])
def test_retry_policy_cannot_be_weakened(policy):
    store = NavigationTaskStore()
    task = store.ensure_task(target(), "pending-key", now=NOW)
    with pytest.raises(ValueError):
        store.begin_episode(task.task_id, now=NOW, **policy)


def failed_observation(store, *, subject=None, conversation_revision=8, now=NOW,
                       error_code="identity_profile_capture_failed"):
    subject = subject or target()
    task = store.ensure_task(subject, f"observe:{subject.conversation_id}:{conversation_revision}", now=now)
    episode = store.begin_episode(task.task_id, now=now).episode
    assert episode is not None
    store.finish_episode(episode.episode_id, status="needs_attention", now=now, error_code=error_code)
    return task, episode


def claim_recovery(store, *, subject=None, conversation_revision=8, global_revision=65,
                   pause_reason=RECOVERY_REASON, now=CLAIM_NOW):
    return store.claim_observation_recovery(subject or target(), conversation_revision=conversation_revision,
        global_revision=global_revision, pause_reason=pause_reason, now=now)


def test_observation_recovery_consumes_once_without_modifying_failure_or_task():
    store = NavigationTaskStore(owner_id="runtime-owner")
    task, episode = failed_observation(store)
    before_task, before_episode = store.get_task(task.task_id), store.get_episode(episode.episode_id)
    receipt = claim_recovery(store)
    assert str(UUID(receipt.claim_id)) == receipt.claim_id and receipt.owner_id == store.owner_id
    assert receipt.failure_episode_id == episode.episode_id and receipt.pause_reason == RECOVERY_REASON
    assert receipt.conversation_revision == 8 and receipt.global_revision == 65 and receipt.claimed_at == CLAIM_NOW
    assert claim_recovery(store) is None
    row = store.connection.execute("SELECT * FROM runtime_nav_observation_recoveries").fetchone()
    assert row["status"] == "consumed" and row["finished_at"] is None
    assert store.get_task(task.task_id) == before_task and store.get_episode(episode.episode_id) == before_episode
    columns = {row[1] for row in store.connection.execute("PRAGMA table_info(runtime_nav_observation_recoveries)")}
    assert not columns & {"lease", "active_chat_lease", "target_json", "message", "screenshot", "hmac", "key"}


def test_crash_and_owner_change_do_not_refund_consumed_recovery(tmp_path):
    path = tmp_path / "nav.db"
    original = NavigationTaskStore(path, owner_id="old-runtime")
    failed_observation(original)
    receipt = claim_recovery(original)
    original.close()  # Crash leaves consumed, no settlement.
    restarted = NavigationTaskStore(path, owner_id="new-runtime")
    assert claim_recovery(restarted) is None
    assert not restarted.finish_observation_recovery(receipt, succeeded=True, now=NOW)
    assert not restarted.finish_observation_recovery(replace(receipt, owner_id=restarted.owner_id), succeeded=True, now=NOW)
    assert restarted.connection.execute("SELECT status FROM runtime_nav_observation_recoveries").fetchone()[0] == "consumed"


def test_atomic_recovery_claim_across_real_connections_has_one_owner(tmp_path):
    path = tmp_path / "nav.db"
    first = NavigationTaskStore(path, owner_id="first")
    second = NavigationTaskStore(path, owner_id="second")
    failed_observation(first)
    gate = threading.Barrier(2)
    def compete(store):
        gate.wait(timeout=3)
        return claim_recovery(store)
    with ThreadPoolExecutor(max_workers=2) as threads:
        futures = [threads.submit(compete, store) for store in (first, second)]
        receipts = [future.result(timeout=5) for future in futures]
    winners = [receipt for receipt in receipts if receipt is not None]
    assert len(winners) == 1
    assert first.connection.execute("SELECT COUNT(*) FROM runtime_nav_observation_recoveries").fetchone()[0] == 1


@pytest.mark.parametrize("change", [
    {"account_id": "other-account"}, {"conversation_id": "other-conversation"},
    {"binding_id": "other-binding"}, {"binding_revision": 3},
    {"display_name": "Other name"}, {"search_aliases": ("Other alias",)}, {"identity_mode": "session_bound"},
])
def test_recovery_requires_exact_registered_failure_target(change):
    store = NavigationTaskStore()
    failed_observation(store)
    assert claim_recovery(store, subject=target(**change)) is None
    assert store.connection.execute("SELECT COUNT(*) FROM runtime_nav_observation_recoveries").fetchone()[0] == 0


@pytest.mark.parametrize("sql,parameters", [
    ("UPDATE runtime_nav_tasks SET account_id=?", ("other-account",)),
    ("UPDATE runtime_nav_tasks SET conversation_id=?", ("other-conversation",)),
    ("UPDATE runtime_nav_tasks SET binding_id=?", ("other-binding",)),
    ("UPDATE runtime_nav_tasks SET binding_revision=?", (3,)),
    ("UPDATE runtime_nav_tasks SET target_json=?", (target(display_name="Changed target").model_dump_json(),)),
    ("UPDATE runtime_nav_tasks SET target_json=?", ("malformed",)),
    ("UPDATE runtime_nav_tasks SET pending_input_key=?", ("observe:test-conversation:7",)),
    ("UPDATE runtime_nav_episodes SET account_id=?", ("other-account",)),
    ("UPDATE runtime_nav_episodes SET binding_id=?", ("other-binding",)),
    ("UPDATE runtime_nav_episodes SET error_code=?", ("navigation_round_failed",)),
    ("UPDATE runtime_nav_episodes SET status=?", ("candidate_opened",)),
    ("UPDATE runtime_nav_episodes SET finished_at=?", (None,)),
    ("UPDATE runtime_nav_episodes SET finished_at=?", ("",)),
    ("UPDATE runtime_nav_episodes SET finished_at=?", ("not-a-time",)),
    ("UPDATE runtime_nav_episodes SET finished_at=?", ("2026-10-02T00:00:11+00:00",)),
])
def test_recovery_rejects_inconsistent_or_unfinished_historical_evidence(sql, parameters):
    store = NavigationTaskStore()
    failed_observation(store)
    store.connection.execute(sql, parameters)
    assert claim_recovery(store) is None


@pytest.mark.parametrize("evidence", ["none", "pending", "running", "different_error"])
def test_recovery_without_typed_finished_failure_is_not_available(evidence):
    store = NavigationTaskStore()
    if evidence in {"pending", "running"}:
        task = store.ensure_task(target(), "observe:test-conversation:8", now=NOW)
        if evidence == "running":
            episode = store.begin_episode(task.task_id, now=NOW).episode
            store.connection.execute("UPDATE runtime_nav_episodes SET error_code=? WHERE episode_id=?",
                                     ("identity_profile_capture_failed", episode.episode_id))
    elif evidence == "different_error":
        failed_observation(store, error_code="identity_profile_capture_revoked")
    assert claim_recovery(store) is None


@pytest.mark.parametrize("reason", ["human_pause", "ui_automation_unavailable:identity_profile_capture_revoked",
                                   "ui_automation_unavailable:identity_profile_capture_failed:extra", "", None])
def test_recovery_only_accepts_the_original_exact_pause_reason(reason):
    store = NavigationTaskStore()
    failed_observation(store)
    assert claim_recovery(store, pause_reason=reason) is None


def test_round_failed_does_not_hide_original_failure_or_mint_another_chance():
    store = NavigationTaskStore()
    _, original = failed_observation(store)
    failed_observation(store, now=NOW + timedelta(seconds=10), error_code="navigation_round_failed")
    receipt = claim_recovery(store, now=NOW + timedelta(seconds=20))
    assert receipt.failure_episode_id == original.episode_id
    failed_observation(store, now=NOW + timedelta(seconds=20))
    assert claim_recovery(store, now=NOW + timedelta(seconds=30)) is None


def test_conversation_and_control_revision_changes_cannot_reissue_binding_quota():
    store = NavigationTaskStore()
    failed_observation(store)
    receipt = claim_recovery(store)
    assert store.finish_observation_recovery(receipt, succeeded=False, now=CLAIM_NOW)
    failed_observation(store, conversation_revision=9, now=NOW + timedelta(seconds=10))
    assert claim_recovery(store, conversation_revision=9, global_revision=66, now=NOW + timedelta(seconds=10)) is None
    assert claim_recovery(store, global_revision=67, now=NOW + timedelta(seconds=10)) is None
    row = store.connection.execute("SELECT * FROM runtime_nav_observation_recoveries").fetchone()
    assert row["status"] == "failed" and row["conversation_revision"] == 8 and row["global_revision"] == 65


def test_actual_binding_revision_change_needs_its_own_failure_then_gets_separate_quota():
    store = NavigationTaskStore()
    failed_observation(store)
    first = claim_recovery(store)
    revised = target(binding_revision=3)
    assert claim_recovery(store, subject=revised) is None
    _, episode = failed_observation(store, subject=revised, conversation_revision=9, now=NOW + timedelta(seconds=10))
    second = claim_recovery(store, subject=revised, conversation_revision=9, global_revision=66, now=NOW + timedelta(seconds=20))
    assert second is not None and second.claim_id != first.claim_id and second.failure_episode_id == episode.episode_id
    assert second.binding_revision == 3


@pytest.mark.parametrize("field,value", [
    ("claim_id", "other-claim"), ("owner_id", "other-owner"), ("account_id", "other-account"),
    ("conversation_id", "other-conversation"), ("binding_id", "other-binding"), ("binding_revision", 3),
    ("conversation_revision", 9), ("global_revision", 66), ("pause_reason", "human_pause"),
    ("failure_episode_id", "other-episode"), ("claimed_at", CLAIM_NOW + timedelta(microseconds=1)),
])
def test_recovery_settlement_cannot_use_a_foreign_or_modified_receipt(field, value):
    store = NavigationTaskStore()
    failed_observation(store)
    receipt = claim_recovery(store)
    assert not store.finish_observation_recovery(replace(receipt, **{field: value}), succeeded=True, now=CLAIM_NOW + timedelta(seconds=1))
    assert store.connection.execute("SELECT status FROM runtime_nav_observation_recoveries").fetchone()[0] == "consumed"


@pytest.mark.parametrize("succeeded", [True, False])
def test_exact_recovery_settlement_is_terminal_and_never_refunds(succeeded):
    store = NavigationTaskStore()
    failed_observation(store)
    receipt = claim_recovery(store)
    finished = CLAIM_NOW + timedelta(seconds=1)
    assert not store.finish_observation_recovery(receipt, succeeded=succeeded, now=CLAIM_NOW - timedelta(seconds=1))
    assert store.finish_observation_recovery(receipt, succeeded=succeeded, now=finished)
    assert not store.finish_observation_recovery(receipt, succeeded=not succeeded, now=CLAIM_NOW + timedelta(seconds=2))
    assert claim_recovery(store, now=CLAIM_NOW + timedelta(seconds=3)) is None
    row = store.connection.execute("SELECT status,finished_at FROM runtime_nav_observation_recoveries").fetchone()
    assert tuple(row) == ("succeeded" if succeeded else "failed", finished.isoformat())


@pytest.mark.parametrize("values", [{"conversation_revision": -1}, {"conversation_revision": True},
                                   {"global_revision": -1}, {"global_revision": "65"}])
def test_recovery_does_not_accept_untyped_revision_scope(values):
    store = NavigationTaskStore()
    failed_observation(store)
    with pytest.raises(ValueError, match="revisions"):
        claim_recovery(store, **values)


def test_recovery_waits_for_original_failure_cooldown_without_consuming_or_changing_journals():
    store = NavigationTaskStore()
    task, episode = failed_observation(store)
    before_task, before_episode = store.get_task(task.task_id), store.get_episode(episode.episode_id)
    assert claim_recovery(store, now=NOW + timedelta(seconds=5)) is None
    assert store.connection.execute("SELECT COUNT(*) FROM runtime_nav_observation_recoveries").fetchone()[0] == 0
    assert store.get_task(task.task_id) == before_task and store.get_episode(episode.episode_id) == before_episode
    assert claim_recovery(store, now=NOW + timedelta(seconds=10)) is not None


def test_recovery_keeps_existing_three_failures_per_300_second_limit():
    store = NavigationTaskStore()
    for seconds in (0, 10, 20):
        failed_observation(store, now=NOW + timedelta(seconds=seconds))
    for seconds in (30, 299):
        assert claim_recovery(store, now=NOW + timedelta(seconds=seconds)) is None
        assert store.connection.execute("SELECT COUNT(*) FROM runtime_nav_observation_recoveries").fetchone()[0] == 0
    receipt = claim_recovery(store, now=NOW + timedelta(seconds=300))
    assert receipt is not None and receipt.claimed_at == NOW + timedelta(seconds=300)


def test_recovery_cannot_bypass_contact_cooldown_via_other_pending_work():
    store = NavigationTaskStore()
    original, _ = failed_observation(store)
    other = store.ensure_task(target(), "another-pending-key", now=CLAIM_NOW)
    episode = store.begin_episode(other.task_id, now=CLAIM_NOW).episode
    store.finish_episode(episode.episode_id, status="retry_wait", now=CLAIM_NOW, error_code="navigation_round_failed")
    assert store.get_task(original.task_id).retry_at == CLAIM_NOW
    assert claim_recovery(store, now=NOW + timedelta(seconds=15)) is None
    assert claim_recovery(store, now=NOW + timedelta(seconds=20)) is not None


def test_recovery_checks_original_task_retry_at_even_after_contact_latest_has_expired():
    store = NavigationTaskStore()
    task, _ = failed_observation(store)
    later = NOW + timedelta(seconds=30)
    store.connection.execute("UPDATE runtime_nav_tasks SET retry_at=? WHERE task_id=?", (later.isoformat(), task.task_id))
    assert claim_recovery(store, now=NOW + timedelta(seconds=20)) is None
    assert claim_recovery(store, now=later) is not None


def test_recovery_running_and_expired_reservation_cooldown_are_read_only():
    store = NavigationTaskStore()
    failed_observation(store)
    other = store.ensure_task(target(), "another-pending-key", now=CLAIM_NOW)
    running = store.begin_episode(other.task_id, now=CLAIM_NOW).episode
    for seconds in (11, 55, 64):
        assert claim_recovery(store, now=NOW + timedelta(seconds=seconds)) is None
        assert store.connection.execute("SELECT COUNT(*) FROM runtime_nav_observation_recoveries").fetchone()[0] == 0
        assert store.get_episode(running.episode_id).status == "running"  # No abandoned rewrite.
        assert store.get_task(other.task_id).status == "running"
    assert claim_recovery(store, now=NOW + timedelta(seconds=65)) is not None
    assert store.get_episode(running.episode_id).status == "running"
