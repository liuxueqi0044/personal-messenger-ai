"""A file reader sees complete current facts, within a non-renewable lease."""
import asyncio
from datetime import UTC, datetime, timedelta
import os

import pytest

from messenger_ai.adapters.qq.navigation.contracts import ContactTarget
from messenger_ai.adapters.qq.navigation.windows_backend import NavigationGuardState
from messenger_ai.runtime.qq_guard import QQNavigationGuardPublisher, QQGuardPublicationError


class Facts:
    def __init__(self):
        self.now = datetime.now(UTC)
        self.tick = 0
        self.state = NavigationGuardState(target=ContactTarget(account_id="account", conversation_id="conversation",
            binding_id="binding", binding_revision=7, display_name="Registered", identity_mode="persistent"),
            run_id="run", session_epoch="session", surface_epoch="surface", worker_epoch="worker",
            observation_epoch="observation", desktop_lease_id="lease", lease_expires_at=self.now+timedelta(seconds=45),
            control_revision=3, process_id=123, window_handle=456, process_started_at_100ns=789,
            paused=False, has_owned_draft=False, has_commit_obligation=False, published_at=self.now)
        self.calls = 0
        self.fail = False

    def snapshot(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("private-contact-secret-do-not-output")
        return self.state.model_copy(update={"published_at": self.now})

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        self.tick += seconds


def publisher(tmp_path, facts, **options):
    return QQNavigationGuardPublisher(tmp_path/"guard.json", facts.state, facts.snapshot,
        interval=options.pop("interval", .01), clock=lambda: facts.now, monotonic_clock=lambda: facts.tick, **options)


def read(pub):
    return NavigationGuardState.model_validate_json(pub.path.read_bytes())


async def changed_file(pub, predicate):
    for _ in range(100):
        if predicate(read(pub)):
            return read(pub)
        await asyncio.sleep(.005)
    raise AssertionError("current snapshot did not reach reader")


@pytest.mark.asyncio
async def test_reader_observes_live_control_and_obligations_without_extending_original_lease(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    original_lease = facts.state.lease_expires_at
    async with pub:
        first = read(pub)
        facts.advance(.1)
        facts.state = facts.state.model_copy(update={"control_revision":4, "paused":True,
            "has_owned_draft":True, "has_commit_obligation":True})
        updated = await changed_file(pub, lambda snapshot: snapshot.control_revision == 4)
        assert updated.paused and updated.has_owned_draft and updated.has_commit_obligation
        assert updated.published_at == facts.now > first.published_at
        assert updated.lease_expires_at == original_lease
        facts.advance(.1)
        facts.state = facts.state.model_copy(update={"control_revision":5, "paused":False,
            "has_owned_draft":False, "has_commit_obligation":False})
        cleared = await changed_file(pub, lambda snapshot: snapshot.control_revision == 5)
        assert not cleared.paused and not cleared.has_owned_draft and not cleared.has_commit_obligation
        assert cleared.lease_expires_at == original_lease
    saved = pub.path.read_bytes()
    await asyncio.sleep(.03)
    assert pub.path.read_bytes() == saved  # Close stops publication, never deletes the file.
    pub.raise_if_failed()


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("run_id","other"), ("session_epoch","other"), ("surface_epoch","other"), ("worker_epoch","other"),
    ("observation_epoch","other"), ("desktop_lease_id","other"), ("process_id",999), ("window_handle",999),
    ("process_started_at_100ns",999),
])
async def test_window_replacement_or_epoch_drift_stops_without_republishing_old_ownership(tmp_path, field, value):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    await pub.start()
    saved = pub.path.read_bytes()
    facts.state = facts.state.model_copy(update={field:value})
    await asyncio.wait_for(pub.failed_event.wait(), timeout=.3)
    with pytest.raises(QQGuardPublicationError, match="scope_changed"):
        pub.raise_if_failed()
    assert pub.path.read_bytes() == saved
    await pub.aclose()


@pytest.mark.asyncio
async def test_target_binding_drift_and_model_copy_boolean_revision_are_not_accepted(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    facts.state = facts.state.model_copy(update={"target": facts.state.target.model_copy(update={"binding_revision":True})})
    with pytest.raises(QQGuardPublicationError, match="snapshot_invalid"):
        await pub.start()
    assert not pub.path.exists() and pub.failed_event.is_set()
    await pub.aclose()


@pytest.mark.asyncio
async def test_control_revision_cannot_regress_even_with_more_restrictive_flags(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    await pub.start()
    saved = pub.path.read_bytes()
    facts.state = facts.state.model_copy(update={"control_revision":2,"paused":True})
    await asyncio.wait_for(pub.failed_event.wait(), timeout=.3)
    with pytest.raises(QQGuardPublicationError, match="control_regressed"):
        pub.raise_if_failed()
    assert pub.path.read_bytes() == saved
    await pub.aclose()


@pytest.mark.asyncio
async def test_shortened_lease_can_never_be_restored_to_original_expiry(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    await pub.start()
    original = facts.state.lease_expires_at
    shortened = facts.now+timedelta(seconds=10)
    facts.state = facts.state.model_copy(update={"lease_expires_at":shortened})
    await changed_file(pub, lambda snapshot: snapshot.lease_expires_at == shortened)
    saved = pub.path.read_bytes()
    facts.state = facts.state.model_copy(update={"lease_expires_at":original})
    await asyncio.wait_for(pub.failed_event.wait(), timeout=.3)
    with pytest.raises(QQGuardPublicationError, match="scope_changed"):
        pub.raise_if_failed()
    assert pub.path.read_bytes() == saved
    await pub.aclose()


@pytest.mark.asyncio
async def test_permission_retry_rechecks_actual_flags_and_atomic_reader_always_sees_whole_json(tmp_path, monkeypatch):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    pub.path.write_text(facts.state.model_dump_json(), encoding="utf-8")
    old_bytes = pub.path.read_bytes()
    replace, calls = os.replace, []
    def retry(source, target):
        # Destination remains a complete older snapshot until atomic replace.
        calls.append(NavigationGuardState.model_validate_json(pub.path.read_bytes()))
        incoming = NavigationGuardState.model_validate_json(source.read_bytes())
        if len(calls) == 1:
            assert pub.path.read_bytes() == old_bytes and incoming.control_revision == 3
            facts.advance(.1)
            facts.state = facts.state.model_copy(update={"paused":True,"control_revision":4})
            raise PermissionError("private-Windows-handle-details")
        assert incoming.paused and incoming.control_revision == 4
        return replace(source, target)
    monkeypatch.setattr("messenger_ai.runtime.qq_guard.os.replace", retry)
    await pub.start()
    assert read(pub).paused and read(pub).control_revision == 4
    assert facts.calls == 2
    assert read(pub).lease_expires_at == facts.state.lease_expires_at
    await pub.aclose()
    assert not list(tmp_path.glob(".qq-guard-*.tmp"))


@pytest.mark.asyncio
async def test_persistent_replace_failure_prevents_spawn_and_preserves_previous_guard(tmp_path, monkeypatch):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    pub.path.write_text(facts.state.model_dump_json(), encoding="utf-8")
    saved = pub.path.read_bytes()
    def denied(*args):
        raise PermissionError("secret-path-do-not-output")
    monkeypatch.setattr("messenger_ai.runtime.qq_guard.os.replace", denied)
    spawned = False
    with pytest.raises(QQGuardPublicationError, match="replace_failed") as caught:
        async with pub:
            spawned = True
    assert not spawned and pub.failed_event.is_set()
    assert "secret" not in str(caught.value) and pub.path.read_bytes() == saved
    assert not list(tmp_path.glob(".qq-guard-*.tmp"))
    await pub.aclose()


@pytest.mark.asyncio
async def test_expired_initial_lease_never_produces_a_fresh_guard(tmp_path):
    facts = Facts()
    facts.state = facts.state.model_copy(update={"lease_expires_at":facts.now})
    pub = publisher(tmp_path, facts)
    with pytest.raises(QQGuardPublicationError, match="lease_expired"):
        await pub.start()
    assert not pub.path.exists()
    await pub.aclose()


@pytest.mark.asyncio
async def test_expiry_during_windows_retry_preserves_old_file_and_never_renews_lease(tmp_path, monkeypatch):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    pub.path.write_text(facts.state.model_dump_json(), encoding="utf-8")
    saved = pub.path.read_bytes()
    def denied(*args):
        facts.advance(45)
        raise PermissionError("sharing violation")
    monkeypatch.setattr("messenger_ai.runtime.qq_guard.os.replace", denied)
    with pytest.raises(QQGuardPublicationError, match="lease_expired"):
        await pub.start()
    assert pub.path.read_bytes() == saved
    await pub.aclose()


@pytest.mark.asyncio
async def test_stale_source_timestamp_or_untyped_dict_cannot_be_relabelled_fresh(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    pub.live_snapshot = lambda: facts.state.model_copy(update={"published_at":facts.now-timedelta(seconds=6)})
    with pytest.raises(QQGuardPublicationError, match="source_stale"):
        await pub.start()
    assert not pub.path.exists()
    await pub.aclose()
    other = publisher(tmp_path, facts)
    other.live_snapshot = lambda: facts.state.model_dump(exclude={"paused"})
    with pytest.raises(QQGuardPublicationError, match="snapshot_invalid"):
        await other.start()
    assert not other.path.exists()
    await other.aclose()


@pytest.mark.asyncio
async def test_utc_rollback_cannot_extend_monotonic_lease_or_hide_background_failure(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    await pub.start()
    saved = pub.path.read_bytes()
    facts.tick = 46
    facts.now -= timedelta(seconds=10)
    await asyncio.wait_for(pub.failed_event.wait(), timeout=.3)
    with pytest.raises(QQGuardPublicationError, match="lease_expired"):
        pub.raise_if_failed()
    assert pub.path.read_bytes() == saved
    await pub.aclose()


@pytest.mark.asyncio
async def test_missed_five_second_freshness_window_cannot_restart_same_guard_heartbeat(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    await pub.start()
    saved = pub.path.read_bytes()
    facts.advance(6)
    await asyncio.wait_for(pub.failed_event.wait(), timeout=.3)
    with pytest.raises(QQGuardPublicationError, match="publication_stale"):
        pub.raise_if_failed()
    assert pub.path.read_bytes() == saved
    await pub.aclose()


@pytest.mark.asyncio
async def test_source_failure_is_content_free_stops_publication_and_wakes_parent(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    await pub.start()
    saved = pub.path.read_bytes()
    facts.fail = True
    await asyncio.wait_for(pub.failed_event.wait(), timeout=.3)
    with pytest.raises(QQGuardPublicationError, match="source_failed") as caught:
        pub.raise_if_failed()
    assert "private-contact" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert pub.path.read_bytes() == saved
    await pub.aclose()


@pytest.mark.asyncio
async def test_cancel_before_background_task_enters_still_wakes_supervision(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    await pub.start()
    saved = pub.path.read_bytes()
    pub._task.cancel()
    await asyncio.wait_for(pub.failed_event.wait(), timeout=.3)
    with pytest.raises(QQGuardPublicationError, match="publication_cancelled"):
        pub.raise_if_failed()
    assert pub.path.read_bytes() == saved
    await pub.aclose()


@pytest.mark.asyncio
async def test_close_immediately_after_start_stops_cleanly_and_is_idempotent(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts)
    await pub.start()
    saved = pub.path.read_bytes()
    await pub.aclose()
    await pub.aclose()
    pub.raise_if_failed()
    assert pub.path.read_bytes() == saved


@pytest.mark.parametrize("interval", [0, 1.01, float("inf"), float("nan"), True])
def test_invalid_publish_interval_cannot_claim_five_second_freshness(tmp_path, interval):
    with pytest.raises(ValueError):
        publisher(tmp_path, Facts(), interval=interval)


def test_relative_output_path_and_unvalidated_initial_flags_are_rejected(tmp_path):
    facts = Facts()
    with pytest.raises(ValueError):
        QQNavigationGuardPublisher("guard.json", facts.state, facts.snapshot)
    with pytest.raises(QQGuardPublicationError, match="snapshot_invalid"):
        QQNavigationGuardPublisher(
            tmp_path/"guard.json", facts.state.model_copy(update={"paused":"false"}), facts.snapshot)


async def test_publish_now_flushes_commit_facts_before_next_heartbeat_without_renewal(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts, interval=1)
    await pub.start()
    original = read(pub)
    facts.advance(.1)
    facts.state = facts.state.model_copy(update={"has_owned_draft":True,
        "has_commit_obligation":True, "control_revision":4})
    await pub.publish_now()
    flushed = read(pub)
    assert flushed.has_owned_draft and flushed.has_commit_obligation
    assert flushed.control_revision == 4 and flushed.published_at == facts.now
    assert flushed.lease_expires_at == original.lease_expires_at
    await pub.aclose()


async def test_explicit_flush_serializes_with_sharing_retry_and_rereads_latest_source(tmp_path, monkeypatch):
    facts = Facts()
    pub = publisher(tmp_path, facts, interval=1)
    await pub.start()
    original_replace = os.replace
    sharing = asyncio.Event()
    versions = []
    calls = 0
    def replace(source, destination):
        nonlocal calls
        calls += 1
        if calls == 1:
            sharing.set()
            raise PermissionError("private sharing details")
        versions.append(NavigationGuardState.model_validate_json(open(source, "rb").read()).control_revision)
        original_replace(source, destination)
    monkeypatch.setattr(os, "replace", replace)
    facts.state = facts.state.model_copy(update={"control_revision":4})
    first = asyncio.create_task(pub.publish_now())
    await sharing.wait()
    facts.state = facts.state.model_copy(update={"control_revision":5, "has_commit_obligation":True})
    second = asyncio.create_task(pub.publish_now())
    await asyncio.gather(first, second)
    assert versions == [5, 5]
    assert read(pub).has_commit_obligation and read(pub).control_revision == 5
    await pub.aclose()


async def test_explicit_flush_failure_notifies_supervisor_and_does_not_refresh_old_file(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts, interval=1)
    await pub.start()
    saved = pub.path.read_bytes()
    facts.fail = True
    with pytest.raises(QQGuardPublicationError, match="qq_guard_source_failed"):
        await pub.publish_now()
    assert pub.failed_event.is_set() and pub.path.read_bytes() == saved
    await pub.aclose()


async def test_cancelled_flush_during_sharing_retry_revokes_publisher(tmp_path, monkeypatch):
    facts = Facts()
    pub = publisher(tmp_path, facts, interval=1)
    await pub.start()
    saved = pub.path.read_bytes()
    sharing = asyncio.Event()
    def replace(*args):
        sharing.set()
        raise PermissionError("private sharing details")
    monkeypatch.setattr(os, "replace", replace)
    flush = asyncio.create_task(pub.publish_now())
    await sharing.wait()
    flush.cancel()
    with pytest.raises(asyncio.CancelledError):
        await flush
    assert pub.failed_event.is_set() and pub.path.read_bytes() == saved
    await pub.aclose()


async def test_flush_requires_first_success_and_cannot_extend_shortened_lease(tmp_path):
    facts = Facts()
    pub = publisher(tmp_path, facts, interval=1)
    with pytest.raises(QQGuardPublicationError, match="not_started"):
        await pub.publish_now()
    await pub.start()
    expiry = facts.state.lease_expires_at
    facts.state = facts.state.model_copy(update={"lease_expires_at":expiry-timedelta(seconds=5)})
    await pub.publish_now()
    saved = pub.path.read_bytes()
    facts.state = facts.state.model_copy(update={"lease_expires_at":expiry})
    with pytest.raises(QQGuardPublicationError, match="scope_changed"):
        await pub.publish_now()
    assert pub.failed_event.is_set() and pub.path.read_bytes() == saved
    await pub.aclose()
