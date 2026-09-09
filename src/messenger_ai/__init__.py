"""Personal Messenger AI domain kernel (M0)."""

from .domain.clock import ClockPort, SystemClock
from .domain.errors import DomainError, ErrorCode
from .domain.models import *

__all__ = ["ClockPort", "DomainError", "ErrorCode", "SystemClock"]
