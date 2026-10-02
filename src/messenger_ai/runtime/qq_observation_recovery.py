"""One complete read recovery; it never authorizes drafting or sending."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime


PROFILE_CAPTURE_PAUSE = "ui_automation_unavailable:identity_profile_capture_failed"


class _RecoveryRound:
    def __init__(self, recovery, context):
        self.recovery, self.context = recovery, context

    def finish(self, batch, events, acknowledged_count) -> bool:
        recovery, context = self.recovery, self.context
        if (not recovery.clear_for_read(context.receipt.account_id)
                or recovery.app._pause_requested.is_set()
                or not batch.complete or batch.gap_reason is not None):
            return False
        keys = tuple(message.local_message_key for message in batch.messages)
        if len(keys) != len(set(keys)):
            return False
        # ACK may silently update fewer rows, or all rows may already have
        # been delivered. The durable exact-key facts decide, not its count.
        for key in keys:
            row = recovery.bridge._cursor.connection.execute(
                "SELECT status FROM observation_outbox WHERE conversation_id=? AND local_key=?",
                (batch.conversation_id, key)).fetchone()
            if row is None or row["status"] != "delivered":
                return False
        return recovery.app.state.finish_observation_recovery(context, batch)


class QQHybridObservationRecovery:
    def __init__(self, *, app, bridge, navigation):
        self.app, self.bridge, self.navigation = app, bridge, navigation

    def clear_for_read(self, account_id: str) -> bool:
        nav, bridge = self.navigation, self.bridge
        session = bridge._worker.status_snapshot()
        if (session.state != "idle" or session.cleanup_required or nav._closed or nav._active
                or nav._rounds or nav.journal.held(account_id)
                or bridge.has_cleanup_obligation(account_id)):
            return False
        # An uncertain committed send is a hard obligation even when the
        # general bridge regards its operation as terminal.
        for binding in bridge._bindings.values():
            if binding.account_id != account_id:
                continue
            if bridge._db.execute("""SELECT 1 FROM qq_vm_ops WHERE binding_id=?
                AND commit_intent=1 AND status!='verified' LIMIT 1""",
                (binding.binding_id,)).fetchone():
                return False
        return nav.store.connection.execute(
            "SELECT 1 FROM runtime_nav_episodes WHERE account_id=? AND status='running' LIMIT 1",
            (account_id,)).fetchone() is None

    @asynccontextmanager
    async def context(self, conversation_id, *, binding_revision, conversation_revision):
        app, bridge, nav = self.app, self.bridge, self.navigation
        binding = bridge._bindings[conversation_id]
        row = app.state.connection.execute(
            "SELECT * FROM runtime_conversations WHERE conversation_id=?", (conversation_id,)).fetchone()
        br, cr, _paused, gr, gp = app.state.execution_state(conversation_id)
        eligible = bool(row and row["paused"] and row["pause_reason"] == PROFILE_CAPTURE_PAUSE
            and row["account_id"] == binding.account_id and row["contact_id"] == binding.contact_id
            and row["conversation_type"] == "direct" and (br, cr) == (binding_revision, conversation_revision)
            and not gp and not app._pause_requested.is_set()
            and bridge._cursor.has_snapshot(conversation_id)
            and self.clear_for_read(binding.account_id))
        if not eligible:
            yield None
            return
        receipt = nav.store.claim_observation_recovery(nav.settings.targets[binding.binding_id],
            conversation_revision=cr, global_revision=gr, pause_reason=PROFILE_CAPTURE_PAUSE,
            now=datetime.now(UTC))
        if receipt is None:
            yield None
            return
        recovered = False
        try:
            # Admission is checked again after consuming the non-refundable
            # budget, before any guard can mask the exact automatic pause.
            if not self.clear_for_read(binding.account_id) or app._pause_requested.is_set():
                yield None
                return
            with app.state.observation_recovery_scope(receipt, contact_id=binding.contact_id) as context:
                yield _RecoveryRound(self, context)
                recovered = context.phase == "recovered"
        finally:
            # This is an audit settlement, not a cross-database transaction.
            # A failed write leaves "consumed", which still prevents replay.
            nav.store.finish_observation_recovery(receipt, succeeded=recovered, now=datetime.now(UTC))
