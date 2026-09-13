from __future__ import annotations

import argparse

import pytest

from scripts.release_qq_observation_quarantine import execute_release


def test_offline_release_refuses_when_runtime_owns_mutex(tmp_path):
    class OccupiedOwner:
        closed = False

        def acquire(self):
            raise RuntimeError("QQ runtime is already running for this Windows user")

        def close(self):
            self.closed = True

    args = argparse.Namespace(
        sqlite_path=tmp_path / "never-open.sqlite3",
        expected_run_id="00000000-0000-0000-0000-000000000001",
        conversation_id="conversation-1",
        binding_id="binding-1",
        binding_revision=1,
        request_id="00000000-0000-0000-0000-000000000002",
        failed_generation=1,
        operator_id="operator",
        reason_code="host_power_suspend_confirmed",
    )

    with pytest.raises(RuntimeError, match="already running"):
        execute_release(args, owner_factory=OccupiedOwner)

    assert args.sqlite_path.exists() is False
