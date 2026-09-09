from enum import StrEnum


class ErrorCode(StrEnum):
    SAFE_RETRY = "SAFE_RETRY"
    FAILED_SAFE = "FAILED_SAFE"
    SEND_UNCERTAIN = "SEND_UNCERTAIN"
    FOREGROUND_REQUIRED = "FOREGROUND_REQUIRED"
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    STALE_CONTEXT = "STALE_CONTEXT"
    IDENTITY_AMBIGUOUS = "IDENTITY_AMBIGUOUS"
    ADAPTER_QUARANTINED = "ADAPTER_QUARANTINED"
    DUPLICATE = "DUPLICATE"
    INVALID_STATE = "INVALID_STATE"


class DomainError(Exception):
    def __init__(self, code: ErrorCode, message: str = ""):
        self.code, self.message = code, message or code.value
        super().__init__(f"{code.value}: {self.message}")
