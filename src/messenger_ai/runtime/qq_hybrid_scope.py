"""Publish actual runtime facts without granting or renewing UI authority."""
from __future__ import annotations

from datetime import UTC, datetime
import json

from messenger_ai.adapters.qq.navigation.windows_backend import NavigationGuardState


class QQHybridRuntimeScope:
    def __init__(self, *, app_lookup, bridge_lookup, settings, run_id):
        self.app_lookup, self.bridge_lookup = app_lookup, bridge_lookup
        self.settings, self.run_id = settings, run_id

    def _binding(self, target, *, observation_only=False):
        app = self.app_lookup()
        if app is None or self.settings.targets.get(target.binding_id) != target:
            raise RuntimeError("hybrid_runtime_scope_unavailable")
        row = app.state.connection.execute(
            "SELECT account_id,contact_id,conversation_type FROM runtime_conversations WHERE conversation_id=?",
            (target.conversation_id,),
        ).fetchone()
        bridge = self.bridge_lookup()
        binding = bridge._by_id.get(target.binding_id) if bridge is not None else None
        if (row is None or binding is None or row["account_id"] != target.account_id
                or row["contact_id"] != binding.contact_id or row["conversation_type"] != "direct"
                or binding.hub_conversation_id != target.conversation_id):
            raise RuntimeError("hybrid_runtime_binding_changed")
        # A temporary read failure needs another full observation to recover.
        # Reuse the existing observation permission; drafting and commit still
        # read the ordinary blocked state until apply_observation succeeds.
        read_state = (app.state.one_shot_observation_execution_state if observation_only
                      else app.state.execution_state)
        br, cr, paused, gr, gp = read_state(target.conversation_id)
        if br != target.binding_revision:
            raise RuntimeError("hybrid_runtime_binding_changed")
        return app, bridge, br, cr, paused or gp or app._pause_requested.is_set(), gr

    def snapshot(self, *, target, purpose, worker_epoch, deadline_at, desktop_lease_id, observation_epoch):
        app, bridge, _br, _cr, paused, gr = self._binding(
            target, observation_only=purpose in {"navigation", "observe"})
        # These rows precede any IPC mutation. A single currently starting cold
        # reservation has not established an owned composer yet. Foreign or
        # interrupted preparation is an obligation and cannot enter navigation.
        rows = bridge._db.execute(
            "SELECT request_json,status FROM qq_v2_draft_reservations WHERE account_id=? "
            "AND status NOT IN ('cleaned','verified')", (target.account_id,),
        ).fetchall()
        session = bridge._worker.status_snapshot()
        starting_cold = (purpose == "draft" and len(rows) == 1 and rows[0]["status"] == "preparing"
                         and session.state in {"starting", "active"} and not session.cleanup_required
                         and session.purpose == "draft"
                         and str(session.worker_epoch) == str(worker_epoch))
        if starting_cold:
            request = json.loads(rows[0]["request_json"])
            starting_cold = (request["binding_id"] == target.binding_id
                             and request["conversation_id"] == target.conversation_id
                             and request["global_revision"] == gr)
        owned = bool(rows) and not starting_cold
        ids = tuple(x.binding_id for x in bridge._by_id.values() if x.account_id == target.account_id)
        ops = bridge._db.execute(
            "SELECT status,commit_intent FROM qq_vm_ops WHERE binding_id IN ("
            + ",".join("?" for _ in ids) + ")", ids,
        ).fetchall()
        commit = any(bool(row["commit_intent"]) and row["status"] != "verified" for row in ops)
        owned = owned or any(row["status"] in {"prepared", "committed"} for row in ops)
        return NavigationGuardState(
            target=target, run_id=self.run_id, session_epoch=self.settings.session_epoch,
            surface_epoch=self.settings.surface_epoch, worker_epoch=str(worker_epoch),
            observation_epoch=observation_epoch, desktop_lease_id=desktop_lease_id,
            lease_expires_at=deadline_at, control_revision=gr,
            process_id=self.settings.window.process_id, window_handle=self.settings.window.window_handle,
            process_started_at_100ns=self.settings.process_started_at_100ns,
            paused=paused, has_owned_draft=owned, has_commit_obligation=commit,
            published_at=datetime.now(UTC),
        )

    def request_is_current(self, request):
        try:
            target = self.settings.targets[request.binding_id]
            app, _bridge, br, cr, paused, gr = self._binding(target)
            if (paused or (br, cr, gr) != (request.binding_revision, request.conversation_revision,
                                         request.global_revision)):
                return False
            row = app.pacing.connection.execute(
                "SELECT pacing_plan_id,segment_index,status,claim_token FROM m10_due_outbox WHERE outbox_id=?",
                (request.outbox_id,),
            ).fetchone()
            return bool(row and row["claim_token"] == request.claim_token
                        and row["status"] in {"dispatching", "dispatching_nonrecoverable"}
                        and row["pacing_plan_id"] == str(request.pacing_plan_id)
                        and row["segment_index"] == request.segment_index)
        except (KeyError, RuntimeError):
            return False
