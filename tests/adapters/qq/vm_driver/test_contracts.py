"""Contract tests for the one-use, HMAC-authenticated ``SelectionHandoff``.

Expected API (``messenger_ai.adapters.qq.vm_driver.contracts``):

* ``SelectionHandoff`` binds exactly one predecessor outcome to exactly one
  successor command: ``handoff_id``, ``source`` (``selection_refresh`` or
  ``commit_success``), ``source_kind``, ``target_kind``, ``binding_id``,
  ``binding_revision``, ``conversation_revision``, ``operation_id``,
  ``predecessor_request_id``, ``successor_request_id``,
  ``predecessor_worker_epoch``, ``successor_worker_epoch``, the exact target
  runtime-id digest, an aware ``expires_at``, the successor ``deadline``
  (``successor_deadline``) and -- for a ``PREPARE`` successor only -- the bound
  ``text_sha256`` and ``segment_ref``.
* ``mint_selection_handoff(*, predecessor_command, predecessor_result,
  successor_command, source, successor_worker_epoch, target_runtime_id_digest,
  expires_at, signing_key)`` is the only way to create one.  It refuses any
  predecessor that is not an exact, authoritative, correlated outcome for the
  exact successor command, refuses a signing key shorter than 32 bytes, and
  stamps ``auth_tag`` with HMAC-SHA256 over the canonical JSON of every security
  field except ``auth_tag``.
* ``verify_selection_handoff_auth(handoff, signing_key) -> bool`` performs a
  constant-time check of that tag: a hand-built or tampered token never passes.
"""
from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from messenger_ai.adapters.qq.vm_driver.contracts import (
    PreparedTargetIdentity,
    PreparedVerificationEvidence,
    SelectionHandoff,
    WorkerCommand,
    WorkerKind,
    WorkerResult,
    WorkerStatus,
    mint_selection_handoff,
    verify_selection_handoff_auth,
)

# 32 bytes == the documented minimum strength for the HMAC signing key.
KEY = bytes(range(32))
SHORT_KEY = b"x" * 31
OTHER_KEY = bytes(range(1, 33))


def _prepared_evidence() -> PreparedVerificationEvidence:
    return PreparedVerificationEvidence(
        owner_binding_id="approved-binding-1",
        target_identity=PreparedTargetIdentity(
            binding_id="approved-binding-1",
            participant_signature="registered-direct-target",
            conversation_type="direct",
            process_id=101,
            window_handle=1001,
        ),
        before_bubbles=(),
        text_hash="d" * 64,
        segment_ref="segment-1",
    )


def _successor_deadline() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=60)


def _observe_command(
    *, binding_id: str = "approved-binding-1", binding_revision: int = 0,
    conversation_revision: int = 0, deadline: datetime | None = None,
    operation_id: UUID | None = None,
) -> WorkerCommand:
    return WorkerCommand(
        kind=WorkerKind.OBSERVE,
        binding_id=binding_id,
        binding_revision=binding_revision,
        conversation_revision=conversation_revision,
        operation_id=operation_id,
        deadline=deadline if deadline is not None else _successor_deadline(),
    )


def _prepare_command(
    *, binding_id: str = "approved-binding-1", binding_revision: int = 2,
    conversation_revision: int = 3, operation_id: UUID | None = None,
    text: str = "hello there", segment_ref: str = "segment-1",
    deadline: datetime | None = None,
) -> WorkerCommand:
    return WorkerCommand(
        kind=WorkerKind.PREPARE,
        binding_id=binding_id,
        binding_revision=binding_revision,
        conversation_revision=conversation_revision,
        operation_id=operation_id if operation_id is not None else uuid4(),
        segment_ref=segment_ref,
        text=text,
        deadline=deadline if deadline is not None else _successor_deadline(),
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


def _refresh_predecessor_for(successor: WorkerCommand) -> WorkerCommand:
    """The exact same-scope predecessor whose refresh outcome authorises it."""

    return WorkerCommand(
        kind=successor.kind,
        binding_id=successor.binding_id,
        binding_revision=successor.binding_revision,
        conversation_revision=successor.conversation_revision,
        operation_id=successor.operation_id,
        segment_ref=successor.segment_ref,
        text=successor.text,
    )


def _expiry() -> datetime:
    return datetime.now(UTC) + timedelta(seconds=30)


def _mint_refresh(
    *, predecessor: WorkerCommand | None = None, result: WorkerResult | None = None,
    successor: WorkerCommand | None = None, expires_at: datetime | None = None,
    successor_worker_epoch: UUID | None = None, signing_key: bytes = KEY,
) -> SelectionHandoff:
    successor = successor if successor is not None else _observe_command()
    predecessor = (
        predecessor if predecessor is not None else _refresh_predecessor_for(successor)
    )
    predecessor_result = (
        result if result is not None else _refresh_result(predecessor)
    )
    return mint_selection_handoff(
        predecessor_command=predecessor,
        predecessor_result=predecessor_result,
        successor_command=successor,
        source="selection_refresh",
        successor_worker_epoch=(
            successor_worker_epoch
            if successor_worker_epoch is not None
            else uuid4()
        ),
        target_runtime_id_digest="a" * 64,
        expires_at=(
            expires_at if expires_at is not None else successor.deadline
        ),
        signing_key=signing_key,
    )


def _handoff_payload(**overrides) -> dict[str, object]:
    """A structurally valid direct-construction payload for the model."""

    payload: dict[str, object] = {
        "source": "selection_refresh",
        "source_kind": WorkerKind.OBSERVE,
        "target_kind": WorkerKind.OBSERVE,
        "binding_id": "approved-binding-1",
        "binding_revision": 0,
        "conversation_revision": 0,
        "operation_id": None,
        "predecessor_request_id": uuid4(),
        "successor_request_id": uuid4(),
        "predecessor_worker_epoch": uuid4(),
        "successor_worker_epoch": uuid4(),
        "target_runtime_id_digest": "a" * 64,
        "expires_at": _expiry(),
        "successor_deadline": _expiry(),
        "text_sha256": None,
        "segment_ref": None,
        "prepared_evidence_sha256": None,
        "auth_tag": "0" * 64,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# Field binding and happy paths
# ---------------------------------------------------------------------------


def test_selection_handoff_roundtrips_every_bound_field() -> None:
    predecessor = _observe_command(binding_revision=4, conversation_revision=9)
    result = _refresh_result(predecessor)
    successor = _observe_command(binding_revision=4, conversation_revision=9)

    handoff = mint_selection_handoff(
        predecessor_command=predecessor,
        predecessor_result=result,
        successor_command=successor,
        source="selection_refresh",
        successor_worker_epoch=uuid4(),
        target_runtime_id_digest="a" * 64,
        expires_at=_expiry(),
        signing_key=KEY,
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
    assert handoff.successor_worker_epoch != result.worker_epoch
    assert handoff.target_runtime_id_digest == "a" * 64
    # The successor deadline is bound exactly to the command's deadline.
    assert handoff.successor_deadline == successor.deadline
    # A non-PREPARE successor binds no text or segment.
    assert handoff.text_sha256 is None
    assert handoff.segment_ref is None
    # A fresh token carries a valid hex-64 HMAC.
    assert len(handoff.auth_tag) == 64
    int(handoff.auth_tag, 16)
    assert verify_selection_handoff_auth(handoff, KEY) is True

    restored = SelectionHandoff.model_validate(
        json.loads(handoff.model_dump_json())
    )
    assert restored == handoff
    assert restored.handoff_id == handoff.handoff_id
    assert verify_selection_handoff_auth(restored, KEY) is True


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
        prepared_evidence=_prepared_evidence(),
        deadline=_successor_deadline(),
    )

    handoff = mint_selection_handoff(
        predecessor_command=commit,
        predecessor_result=commit_result,
        successor_command=verify,
        source="commit_success",
        successor_worker_epoch=uuid4(),
        target_runtime_id_digest="b" * 64,
        expires_at=verify.deadline,
        signing_key=KEY,
    )

    assert handoff.source == "commit_success"
    assert handoff.source_kind is WorkerKind.COMMIT
    assert handoff.target_kind is WorkerKind.VERIFY
    assert handoff.operation_id == operation_id
    assert handoff.predecessor_request_id == commit.request_id
    assert handoff.successor_request_id == verify.request_id
    assert handoff.predecessor_worker_epoch == commit_result.worker_epoch
    assert handoff.successor_worker_epoch != commit_result.worker_epoch
    assert handoff.target_runtime_id_digest == "b" * 64
    assert handoff.successor_deadline == verify.deadline
    assert handoff.text_sha256 is None
    assert handoff.segment_ref is None
    assert handoff.prepared_evidence_sha256 is not None
    assert verify_selection_handoff_auth(handoff, KEY) is True


def test_prepare_handoff_binds_text_sha256_and_segment_ref() -> None:
    successor = _prepare_command(text="hello there", segment_ref="segment-1")

    handoff = _mint_refresh(successor=successor)

    assert handoff.target_kind is WorkerKind.PREPARE
    assert handoff.text_sha256 == hashlib.sha256(b"hello there").hexdigest()
    assert handoff.segment_ref == "segment-1"
    assert handoff.successor_deadline == successor.deadline
    assert verify_selection_handoff_auth(handoff, KEY) is True


def test_prepare_text_hash_tracks_the_exact_successor_text() -> None:
    first = _mint_refresh(successor=_prepare_command(text="hello there"))
    second = _mint_refresh(successor=_prepare_command(text="something else"))

    assert first.text_sha256 == hashlib.sha256(b"hello there").hexdigest()
    assert second.text_sha256 == hashlib.sha256(b"something else").hexdigest()
    assert first.text_sha256 != second.text_sha256
    # Two different payloads can never share an authenticated tag.
    assert first.auth_tag != second.auth_tag


def test_non_prepare_observe_handoff_binds_no_text_or_segment() -> None:
    handoff = _mint_refresh(successor=_observe_command())
    assert handoff.text_sha256 is None
    assert handoff.segment_ref is None


def test_non_prepare_verify_handoff_binds_no_text_or_segment() -> None:
    operation_id = uuid4()
    commit = WorkerCommand(
        kind=WorkerKind.COMMIT, binding_id="approved-binding-1",
        operation_id=operation_id,
    )
    commit_result = WorkerResult(
        request_id=commit.request_id, kind=WorkerKind.COMMIT,
        status=WorkerStatus.OK, worker_epoch=uuid4(),
        operation_id=operation_id, binding_id=commit.binding_id,
    )
    successor = WorkerCommand(
        kind=WorkerKind.VERIFY, binding_id="approved-binding-1",
        operation_id=operation_id, prepared_evidence=_prepared_evidence(),
        deadline=_successor_deadline(),
    )
    handoff = mint_selection_handoff(
        predecessor_command=commit,
        predecessor_result=commit_result,
        successor_command=successor,
        source="commit_success",
        successor_worker_epoch=uuid4(),
        target_runtime_id_digest="a" * 64,
        expires_at=successor.deadline,
        signing_key=KEY,
    )
    assert handoff.text_sha256 is None
    assert handoff.segment_ref is None
    assert handoff.prepared_evidence_sha256 is not None


@pytest.mark.parametrize(
    "successor",
    [
        _prepare_command(text="", segment_ref="segment-1"),
        _prepare_command(text="hello", segment_ref=""),
    ],
)
def test_prepare_handoff_requires_text_and_segment_to_bind(
    successor: WorkerCommand,
) -> None:
    with pytest.raises(ValueError, match="prepare handoff requires"):
        _mint_refresh(successor=successor)


@pytest.mark.parametrize(
    "overrides",
    [
        {"target_kind": WorkerKind.OBSERVE, "text_sha256": "c" * 64},
        {"target_kind": WorkerKind.VERIFY, "segment_ref": "seg"},
    ],
)
def test_model_refuses_text_binding_on_a_non_prepare_target(overrides) -> None:
    with pytest.raises(ValidationError, match="non-prepare handoff"):
        SelectionHandoff.model_validate(_handoff_payload(**overrides))


def test_model_requires_text_binding_on_a_prepare_target() -> None:
    with pytest.raises(ValidationError, match="prepare handoff must bind"):
        SelectionHandoff.model_validate(
            _handoff_payload(target_kind=WorkerKind.PREPARE)
        )


# ---------------------------------------------------------------------------
# Deadline / expiry binding
# ---------------------------------------------------------------------------


def test_expiry_defaults_to_and_may_precede_the_successor_deadline() -> None:
    successor = _observe_command()
    assert _mint_refresh(successor=successor).successor_deadline == successor.deadline

    earlier = _mint_refresh(
        successor=successor, expires_at=successor.deadline - timedelta(seconds=5)
    )
    assert earlier.expires_at < earlier.successor_deadline
    assert verify_selection_handoff_auth(earlier, KEY) is True


def test_expiry_after_the_successor_deadline_is_refused_by_mint() -> None:
    successor = _observe_command()
    with pytest.raises(ValueError, match="must not exceed the successor deadline"):
        _mint_refresh(
            successor=successor,
            expires_at=successor.deadline + timedelta(seconds=1),
        )


def test_model_refuses_expiry_after_the_successor_deadline() -> None:
    deadline = _expiry()
    with pytest.raises(ValidationError, match="must not exceed the successor deadline"):
        SelectionHandoff.model_validate(
            _handoff_payload(
                expires_at=deadline + timedelta(seconds=10),
                successor_deadline=deadline,
            )
        )


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
        SelectionHandoff.model_validate(
            _handoff_payload(
                expires_at=datetime.now().replace(tzinfo=None),  # noqa: DTZ005
            )
        )


def test_mint_requires_an_aware_successor_deadline() -> None:
    # ``model_copy`` bypasses validation, so the naive deadline really reaches
    # ``mint_selection_handoff`` and must be rejected there.
    naive = _observe_command().model_copy(
        update={"deadline": datetime.now().replace(tzinfo=None)}  # noqa: DTZ005
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        _mint_refresh(successor=naive)


def test_mint_requires_a_present_successor_deadline() -> None:
    missing = _observe_command().model_copy(update={"deadline": None})
    with pytest.raises(ValueError, match="requires a successor deadline"):
        _mint_refresh(successor=missing)


def test_model_refuses_a_naive_successor_deadline() -> None:
    with pytest.raises(ValidationError):
        SelectionHandoff.model_validate(
            _handoff_payload(
                successor_deadline=datetime.now().replace(tzinfo=None),  # noqa: DTZ005
            )
        )


# ---------------------------------------------------------------------------
# Signing-key strength
# ---------------------------------------------------------------------------


def test_signing_key_is_a_required_keyword_argument() -> None:
    parameter = inspect.signature(mint_selection_handoff).parameters["signing_key"]
    assert parameter.default is inspect.Parameter.empty
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize(
    "signing_key",
    [b"", b"short", SHORT_KEY, bytearray(b"y" * 20)],
)
def test_mint_refuses_a_short_signing_key(signing_key) -> None:
    with pytest.raises(ValueError, match="signing key"):
        _mint_refresh(signing_key=signing_key)


@pytest.mark.parametrize("signing_key", ["x" * 32, None, 42])
def test_mint_refuses_a_non_bytes_signing_key(signing_key) -> None:
    with pytest.raises(TypeError, match="signing key must be bytes"):
        _mint_refresh(signing_key=signing_key)


def test_mint_accepts_a_32_byte_key() -> None:
    handoff = _mint_refresh(signing_key=bytes(32))
    assert verify_selection_handoff_auth(handoff, bytes(32)) is True


@pytest.mark.parametrize("signing_key", [b"", SHORT_KEY])
def test_verify_refuses_a_short_signing_key(signing_key) -> None:
    handoff = _mint_refresh()
    with pytest.raises(ValueError, match="signing key"):
        verify_selection_handoff_auth(handoff, signing_key)


@pytest.mark.parametrize("signing_key", ["x" * 32, None])
def test_verify_refuses_a_non_bytes_signing_key(signing_key) -> None:
    handoff = _mint_refresh()
    with pytest.raises(TypeError, match="signing key must be bytes"):
        verify_selection_handoff_auth(handoff, signing_key)


# ---------------------------------------------------------------------------
# HMAC authentication: forgery and tampering
# ---------------------------------------------------------------------------


def test_verify_accepts_the_minted_token_only_with_the_minting_key() -> None:
    handoff = _mint_refresh()
    assert verify_selection_handoff_auth(handoff, KEY) is True
    assert verify_selection_handoff_auth(handoff, OTHER_KEY) is False
    assert verify_selection_handoff_auth(handoff, b"z" * 32) is False


def test_a_hand_built_token_never_verifies() -> None:
    handoff = SelectionHandoff.model_validate(_handoff_payload())

    assert verify_selection_handoff_auth(handoff, KEY) is False


def test_a_token_minted_with_another_key_is_a_forgery() -> None:
    forged = _mint_refresh(signing_key=OTHER_KEY)
    assert verify_selection_handoff_auth(forged, OTHER_KEY) is True
    assert verify_selection_handoff_auth(forged, KEY) is False


_TAMPER_VALUES: dict[str, object] = {
    "handoff_id": uuid4(),
    "source": "commit_success",
    "source_kind": WorkerKind.COMMIT,
    "target_kind": WorkerKind.OBSERVE,
    "binding_id": "tampered-binding",
    "binding_revision": 77,
    "conversation_revision": 88,
    "operation_id": uuid4(),
    "predecessor_request_id": uuid4(),
    "successor_request_id": uuid4(),
    "predecessor_worker_epoch": uuid4(),
    "successor_worker_epoch": uuid4(),
    "target_runtime_id_digest": "b" * 64,
    "expires_at": datetime.now(UTC) + timedelta(seconds=45),
    "successor_deadline": datetime.now(UTC) + timedelta(seconds=90),
    "text_sha256": "c" * 64,
    "segment_ref": "tampered-segment",
    "auth_tag": "d" * 64,
}


@pytest.mark.parametrize("field", sorted(_TAMPER_VALUES))
def test_tampering_any_security_field_breaks_verification(field: str) -> None:
    handoff = _mint_refresh(successor=_prepare_command())
    assert verify_selection_handoff_auth(handoff, KEY) is True

    tampered = handoff.model_copy(update={field: _TAMPER_VALUES[field]})
    assert getattr(tampered, field) != getattr(handoff, field)
    assert verify_selection_handoff_auth(tampered, KEY) is False


def test_a_prepare_token_stops_verifying_once_its_text_digest_moves() -> None:
    handoff = _mint_refresh(successor=_prepare_command(text="hello there"))
    moved = handoff.model_copy(
        update={"text_sha256": hashlib.sha256(b"other text").hexdigest()}
    )
    assert verify_selection_handoff_auth(moved, KEY) is False


def test_utc_normalised_datetimes_do_not_spuriously_break_verification() -> None:
    handoff = _mint_refresh()
    # The same instant expressed in another offset is not a tamper.
    equivalent = handoff.model_copy(update={
        "expires_at": handoff.expires_at.astimezone(timezone(timedelta(hours=8)))
    })
    assert equivalent.expires_at == handoff.expires_at
    assert verify_selection_handoff_auth(equivalent, KEY) is True


# ---------------------------------------------------------------------------
# Correlation and the incumbent model invariants
# ---------------------------------------------------------------------------


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
        _mint_refresh(predecessor=_observe_command(), successor=successor)


def test_mint_refuses_successor_that_already_carries_authority() -> None:
    existing = _mint_refresh()
    successor = _observe_command().model_copy(
        update={"selection_handoff": existing}
    )

    with pytest.raises(ValueError):
        _mint_refresh(predecessor=_observe_command(), successor=successor)


@pytest.mark.parametrize("successor_epoch_kind", ["zero", "predecessor"])
def test_mint_requires_a_distinct_nonzero_successor_worker_epoch(
    successor_epoch_kind: str,
) -> None:
    predecessor = _observe_command()
    result = _refresh_result(predecessor)
    successor_epoch = (
        UUID(int=0)
        if successor_epoch_kind == "zero"
        else result.worker_epoch
    )

    with pytest.raises(ValueError, match="successor worker is not fresh"):
        _mint_refresh(
            predecessor=predecessor,
            result=result,
            successor_worker_epoch=successor_epoch,
        )


@pytest.mark.parametrize("epoch_kind", ["predecessor_zero", "successor_zero", "same"])
def test_selection_handoff_model_requires_distinct_nonzero_worker_epochs(
    epoch_kind: str,
) -> None:
    handoff = _mint_refresh()
    payload = handoff.model_dump()
    if epoch_kind == "predecessor_zero":
        payload["predecessor_worker_epoch"] = UUID(int=0)
    elif epoch_kind == "successor_zero":
        payload["successor_worker_epoch"] = UUID(int=0)
    else:
        payload["successor_worker_epoch"] = handoff.predecessor_worker_epoch

    with pytest.raises(ValidationError, match="distinct nonzero worker epochs"):
        SelectionHandoff.model_validate(payload)


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
        operation_id=command.operation_id, deadline=_successor_deadline(),
    )

    with pytest.raises(ValueError):
        mint_selection_handoff(
            predecessor_command=command,
            predecessor_result=result,
            successor_command=successor,
            source=source,
            successor_worker_epoch=uuid4(),
            target_runtime_id_digest="a" * 64,
            expires_at=_expiry(),
            signing_key=KEY,
        )


# ---------------------------------------------------------------------------
# Process-boundary round trip
# ---------------------------------------------------------------------------


def test_worker_command_round_trips_handoff_across_the_process_boundary() -> None:
    handoff = _mint_refresh(successor=_observe_command())
    command = _observe_command().model_copy(update={"selection_handoff": handoff})

    assert WorkerCommand(
        kind=WorkerKind.OBSERVE, binding_id="approved-binding-1"
    ).selection_handoff is None

    restored = WorkerCommand.model_validate(json.loads(command.model_dump_json()))
    assert restored.selection_handoff == handoff
    assert restored.request_id == command.request_id
    assert verify_selection_handoff_auth(restored.selection_handoff, KEY) is True
