"""Contract tests for the one-use ``SelectionHandoff`` selection authority.

Expected API (``messenger_ai.adapters.qq.vm_driver.contracts``):

* ``SelectionHandoff`` binds exactly one predecessor outcome to exactly one
  successor command: ``handoff_id``, ``source`` (``selection_refresh`` or
  ``commit_success``), ``source_kind``, ``target_kind``, ``binding_id``,
  ``binding_revision``, ``conversation_revision``, ``operation_id``,
  ``predecessor_request_id``, ``successor_request_id``,
  ``predecessor_worker_epoch`` and an aware, future ``expires_at``.
* ``mint_selection_handoff(*, predecessor_command, predecessor_result,
  successor_command, source, expires_at)`` is the only way to create one.  It
  refuses any predecessor that is not an exact, authoritative, correlated
  outcome for the exact successor command.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.vm_driver.contracts import (
    SelectionHandoff,
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
    mint_selection_handoff,
)


def _observe_command(
    *, binding_id: str = "approved-binding-1", binding_revision: int = 0,
    conversation_revision: int = 0,
) -> WorkerCommand:
    return WorkerCommand(
        kind=WorkerKind.OBSERVE,
        binding_id=binding_id,
        binding_revision=binding_revision,
        conversation_revision=conversation_revision,
    )


def _refresh_result(command: WorkerCommand, **overrides) -> WorkerResult:
    values: dict[str, object] = {
        "request_id": command.request_id,
        "kind": command.kind,
        "status": WorkerStatus.FAILED_SAFE,
        "worker_epoch": uuid4(),
        "operation_id": command.operation_id,
        "binding_id": command.binding_id,
        "binding_revision": command.binding_revision,
        "conversation_revision": command.conversation_revision,
        "error_code": "selection_process_refresh_required",
    }
    values.update(overrides)
    return WorkerResult(**values)


def _expiry() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=30)


def _mint_refresh(
    *, predecessor: WorkerCommand | None = None, result: WorkerResult | None = None,
    successor: WorkerCommand | None = None, expires_at: datetime | None = None,
) -> SelectionHandoff:
    predecessor = predecessor or _observe_command()
    return mint_selection_handoff(
        predecessor_command=predecessor,
        predecessor_result=result or _refresh_result(predecessor),
        successor_command=successor or _observe_command(),
        source="selection_refresh",
        expires_at=expires_at or _expiry(),
    )


def test_selection_handoff_roundtrips_every_bound_field() -> None:
    predecessor = _observe_command(binding_revision=4, conversation_revision=9)
    result = _refresh_result(predecessor)
    successor = _observe_command(binding_revision=4, conversation_revision=9)

    handoff = mint_selection_handoff(
        predecessor_command=predecessor,
        predecessor_result=result,
        successor_command=successor,
        source="selection_refresh",
        expires_at=_expiry(),
    )

    assert isinstance(handoff.handoff_id, UUID)
    assert handoff.source == "selection_refresh"
    assert handoff.source_kind is WorkerKind.OBSERVE
    assert handoff.target_kind is WorkerKind.OBSERVE
    assert handoff.binding_id == predecessor.binding_id
    assert handoff.binding_revision == 4
    assert handoff.conversation_revision == 9
    assert handoff.operation_id is None
    assert handoff.predecessor_request_id == predecessor.request_id
    assert handoff.successor_request_id == successor.request_id
    assert handoff.predecessor_worker_epoch == result.worker_epoch

    restored = SelectionHandoff.model_validate(
        json.loads(handoff.model_dump_json())
    )
    assert restored == handoff
    assert restored.handoff_id == handoff.handoff_id


def test_commit_success_handoff_binds_commit_to_verify() -> None:
    operation_id = uuid4()
    commit = WorkerCommand(
        kind=WorkerKind.COMMIT, binding_id="approved-binding-1",
        operation_id=operation_id, binding_revision=2, conversation_revision=3,
    )
    commit_result = WorkerResult(
        request_id=commit.request_id, kind=WorkerKind.COMMIT,
        status=WorkerStatus.OK, worker_epoch=uuid4(),
        operation_id=operation_id, binding_id=commit.binding_id,
        binding_revision=2, conversation_revision=3,
    )
    verify = WorkerCommand(
        kind=WorkerKind.VERIFY, binding_id="approved-binding-1",
        operation_id=operation_id, binding_revision=2, conversation_revision=3,
    )

    handoff = mint_selection_handoff(
        predecessor_command=commit,
        predecessor_result=commit_result,
        successor_command=verify,
        source="commit_success",
        expires_at=_expiry(),
    )

    assert handoff.source == "commit_success"
    assert handoff.source_kind is WorkerKind.COMMIT
    assert handoff.target_kind is WorkerKind.VERIFY
    assert handoff.operation_id == operation_id
    assert handoff.predecessor_request_id == commit.request_id
    assert handoff.successor_request_id == verify.request_id
    assert handoff.predecessor_worker_epoch == commit_result.worker_epoch


@pytest.mark.parametrize("naive", [True, False])
def test_expiry_must_be_aware_and_in_the_future(naive: bool) -> None:
    expires_at = (
        datetime.now().replace(tzinfo=None)  # noqa: DTZ005 - naive on purpose
        if naive
        else datetime.now(UTC) - timedelta(seconds=1)
    )

    with pytest.raises(ValueError):
        _mint_refresh(expires_at=expires_at)

    # The type itself must also refuse a naive expiry.
    with pytest.raises(ValidationError):
        SelectionHandoff(
            source="selection_refresh",
            source_kind=WorkerKind.OBSERVE,
            target_kind=WorkerKind.OBSERVE,
            binding_id="approved-binding-1",
            binding_revision=0,
            conversation_revision=0,
            operation_id=None,
            predecessor_request_id=uuid4(),
            successor_request_id=uuid4(),
            predecessor_worker_epoch=uuid4(),
            expires_at=datetime.now().replace(tzinfo=None),  # noqa: DTZ005
        )


@pytest.mark.parametrize(
    "result_update",
    [
        {"request_id": uuid4()},
        {"kind": WorkerKind.PREPARE},
        {"binding_id": "other-binding"},
        {"binding_revision": 99},
        {"conversation_revision": 99},
        {"worker_epoch": UUID(int=0)},
        {"status": WorkerStatus.OK, "error_code": None},
    ],
)
def test_mint_refuses_uncorrelated_predecessor_result(result_update) -> None:
    predecessor = _observe_command(binding_revision=2, conversation_revision=3)
    result = _refresh_result(predecessor, **result_update)

    with pytest.raises(ValueError):
        _mint_refresh(predecessor=predecessor, result=result)


@pytest.mark.parametrize(
    "successor",
    [
        _observe_command(binding_id="other-binding"),
        _observe_command(binding_revision=7),
        _observe_command(conversation_revision=7),
    ],
)
def test_mint_refuses_successor_target_drift(successor: WorkerCommand) -> None:
    with pytest.raises(ValueError):
        _mint_refresh(successor=successor)


def test_mint_refuses_successor_that_already_carries_authority() -> None:
    predecessor = _observe_command()
    existing = _mint_refresh(predecessor=predecessor)
    successor = _observe_command().model_copy(
        update={"selection_handoff": existing}
    )

    with pytest.raises(ValueError):
        _mint_refresh(predecessor=predecessor, successor=successor)


@pytest.mark.parametrize(
    "command,result,source",
    [
        # A refresh token needs the exact pre-commit refresh outcome.
        (
            WorkerCommand(kind=WorkerKind.PREPARE, binding_id="approved-binding-1"),
            WorkerResult(
                request_id=uuid4(), kind=WorkerKind.PREPARE,
                status=WorkerStatus.FAILED_SAFE, worker_epoch=uuid4(),
                error_code="selection_process_refresh_required",
            ),
            "commit_success",
        ),
        # commit_success requires an OK COMMIT predecessor.
        (
            WorkerCommand(
                kind=WorkerKind.COMMIT, binding_id="approved-binding-1",
                operation_id=uuid4(),
            ),
            None,
            "commit_success",
        ),
    ],
)
def test_mint_refuses_missing_authoritative_outcome(command, result, source) -> None:
    if result is None:
        result = WorkerResult(
            request_id=command.request_id, kind=WorkerKind.COMMIT,
            status=WorkerStatus.FAILED_SAFE, worker_epoch=uuid4(),
            operation_id=command.operation_id, binding_id=command.binding_id,
            error_code="target_drift",
        )
    else:
        result = result.model_copy(update={"request_id": command.request_id})
    successor = WorkerCommand(
        kind=WorkerKind.VERIFY, binding_id=command.binding_id,
        operation_id=command.operation_id,
    )

    with pytest.raises(ValueError):
        mint_selection_handoff(
            predecessor_command=command,
            predecessor_result=result,
            successor_command=successor,
            source=source,
            expires_at=_expiry(),
        )


def test_worker_command_round_trips_handoff_across_the_process_boundary() -> None:
    handoff = _mint_refresh(successor=_observe_command())
    command = _observe_command().model_copy(update={"selection_handoff": handoff})

    assert WorkerCommand(kind=WorkerKind.OBSERVE, binding_id="approved-binding-1").selection_handoff is None

    restored = WorkerCommand.model_validate(json.loads(command.model_dump_json()))
    assert restored.selection_handoff == handoff
    assert restored.request_id == command.request_id
