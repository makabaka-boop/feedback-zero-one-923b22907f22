"""Error taxonomy for the eligibility service.

Every externally visible failure is an ``EligibilityError``.  The ``code`` is a
stable machine readable identifier; HTTP-ish callers can use ``status``.
"""

from __future__ import annotations

from typing import Any


class EligibilityError(Exception):
    """Base class for all rejections raised by the service.

    Parameters
    ----------
    code:
        Stable machine readable error code (never changes once released).
    message:
        Human readable explanation.
    status:
        Suggested HTTP status code for HTTP adapters.
    details:
        Optional structured payload (e.g. offending field).
    """

    status = 400

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        if status is not None:
            self.status = status
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details is not None:
            out["details"] = self.details
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"{type(self).__name__}(code={self.code!r}, message={self.message!r})"


# --------------------------------------------------------------------------
# Concrete error types (one per documented rejection reason).
# --------------------------------------------------------------------------


class InvalidInput(EligibilityError):
    """A single request contained malformed data (bad identifier, JSON, ...)."""

    status = 422

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__("invalid_input", message, details=details)


class DuplicateIdentifier(EligibilityError):
    """An identifier is repeated inside a batch or already exists in its kind."""

    status = 409

    def __init__(self, identifier: str, message: str | None = None) -> None:
        super().__init__(
            "duplicate_identifier",
            message or f"identifier {identifier!r} is duplicated",
            details={"identifier": identifier},
        )


class UnknownObject(EligibilityError):
    """Replace/delete targeted an identifier that does not exist."""

    status = 404

    def __init__(self, kind: str, identifier: str) -> None:
        super().__init__(
            "unknown_object",
            f"{kind} {identifier!r} does not exist",
            details={"kind": kind, "identifier": identifier},
        )


class MeasurementTooLarge(EligibilityError):
    """A measurement exceeds the documented size ceiling."""

    status = 413

    def __init__(self, identifier: str, limit: int) -> None:
        super().__init__(
            "measurement_too_large",
            f"measurement {identifier!r} exceeds {limit} bytes",
            details={"identifier": identifier, "limit": limit},
        )


class UnknownApplication(EligibilityError):
    """An export/query referred to an application identifier we never issued."""

    status = 404

    def __init__(self, application_id: str) -> None:
        super().__init__(
            "unknown_application",
            f"application {application_id!r} does not exist",
            details={"application_id": application_id},
        )


class ApplicationConflict(EligibilityError):
    """A retry used the same identifier with a different purpose."""

    status = 409

    def __init__(self, application_id: str, recorded: str, requested: str) -> None:
        super().__init__(
            "application_conflict",
            (
                f"application {application_id!r} was accepted for purpose "
                f"{recorded!r}, retry requested {requested!r}"
            ),
            details={
                "application_id": application_id,
                "recorded_purpose": recorded,
                "requested_purpose": requested,
            },
        )


class StoreBusy(EligibilityError):
    """An internal lock could not be acquired within the retry budget."""

    status = 503

    def __init__(self, message: str = "internal store is busy; retry later") -> None:
        super().__init__("store_busy", message)


