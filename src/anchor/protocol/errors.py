"""Error codes carried by IPC responses (SPEC 5.2).

Every failure crosses the socket as ``{"ok": false, "error": {"code", "message"}}``.
Codes are stable identifiers that clients may branch on; messages are for
humans and may change. ``RATCHET_VIOLATION`` is named by the specification
itself (SPEC 7.4); the rest follow the same style.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable error identifiers."""

    # Protocol-level problems.
    BAD_REQUEST = "BAD_REQUEST"
    UNSUPPORTED_VERSION = "UNSUPPORTED_VERSION"
    UNKNOWN_TYPE = "UNKNOWN_TYPE"
    UNAUTHORIZED = "UNAUTHORIZED"

    # Session-level refusals.
    RATCHET_VIOLATION = "RATCHET_VIOLATION"
    NO_ACTIVE_SESSION = "NO_ACTIVE_SESSION"
    SESSION_ALREADY_ACTIVE = "SESSION_ALREADY_ACTIVE"
    DURATION_TOO_LONG = "DURATION_TOO_LONG"
    INVALID_DURATION = "INVALID_DURATION"
    CANCEL_FORBIDDEN = "CANCEL_FORBIDDEN"
    WAIT_NOT_ELAPSED = "WAIT_NOT_ELAPSED"
    PHRASE_MISMATCH = "PHRASE_MISMATCH"
    VALVE_NOT_REQUESTED = "VALVE_NOT_REQUESTED"
    SKIP_LIMIT_REACHED = "SKIP_LIMIT_REACHED"
    SKIP_FORBIDDEN = "SKIP_FORBIDDEN"

    # Configuration problems.
    UNKNOWN_PROFILE = "UNKNOWN_PROFILE"
    UNKNOWN_SCHEDULE = "UNKNOWN_SCHEDULE"
    INVALID_CONFIG = "INVALID_CONFIG"

    # Integrity and internals.
    INTEGRITY_FAILURE = "INTEGRITY_FAILURE"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    INTERNAL = "INTERNAL"


class AnchorError(Exception):
    """An error that can be reported to a client as a protocol response."""

    code: ErrorCode = ErrorCode.INTERNAL

    def __init__(self, message: str, *, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code

    def as_payload(self) -> dict[str, str]:
        return {"code": str(self.code), "message": self.message}


class ProtocolError(AnchorError):
    """The message itself was malformed, unknown or not allowed."""

    code = ErrorCode.BAD_REQUEST


class UnauthorizedError(AnchorError):
    """The peer is neither root nor the owner UID (SPEC 5.2)."""

    code = ErrorCode.UNAUTHORIZED


class RatchetViolationError(AnchorError):
    """A change that would loosen an active session was rejected (SPEC 7.4)."""

    code = ErrorCode.RATCHET_VIOLATION


class IntegrityError(AnchorError):
    """Stored state failed its HMAC check (SPEC 6.1)."""

    code = ErrorCode.INTEGRITY_FAILURE
