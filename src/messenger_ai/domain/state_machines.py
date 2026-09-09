from .errors import DomainError, ErrorCode
from .models import DraftStatus, SendOperation, SendStatus


class DraftStateMachine:
    @staticmethod
    def authorize(draft):
        if draft.status != DraftStatus.CREATED:
            raise DomainError(ErrorCode.INVALID_STATE)
        return draft.authorize()


class SendStateMachine:
    @staticmethod
    def transition(
        op: SendOperation, target: SendStatus, error: str | None = None
    ) -> SendOperation:
        # Uncertain is terminal for automated flow: no transition to prepared/committed.
        if op.status == SendStatus.UNCERTAIN and target != SendStatus.UNCERTAIN:
            raise DomainError(
                ErrorCode.SEND_UNCERTAIN,
                "uncertain operation requires human reconciliation",
            )
        allowed = {
            SendStatus.PENDING: {
                SendStatus.PREPARED,
                SendStatus.FAILED,
                SendStatus.CANCELLED,
            },
            SendStatus.PREPARED: {
                SendStatus.COMMITTED,
                SendStatus.FAILED,
                SendStatus.UNCERTAIN,
            },
            SendStatus.COMMITTED: {SendStatus.VERIFIED, SendStatus.UNCERTAIN},
            SendStatus.VERIFIED: set(),
            SendStatus.FAILED: set(),
            SendStatus.CANCELLED: set(),
            SendStatus.UNCERTAIN: set(),
        }
        if target not in allowed[op.status]:
            raise DomainError(ErrorCode.INVALID_STATE, f"{op.status}->{target}")
        op.status = target
        op.error_code = error
        return op
