from datetime import UTC, datetime, timedelta
import sqlite3

import pytest

from messenger_ai.adapters.qq.navigation.contracts import ContactTarget
from messenger_ai.runtime.navigation_state import NavigationTaskStore


NOW = datetime(2026, 10, 2, tzinfo=UTC)


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
