"""Pure validation helpers shared by the storage and service layers.

Keeping validation side-effect free makes it easy to reject a whole batch
*before* touching any persisted state, which is part of the contract: an
illegal request must never change state.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .errors import InvalidInput, MeasurementTooLarge

#: Identifiers and purposes are non-empty runs of ASCII letters and digits.
_ID_RE = re.compile(r"\A[A-Za-z0-9]+\Z")
MAX_IDENTIFIER_LENGTH = 512

#: Canonical measurements are at most 1 MiB of canonical UTF-8 JSON.
MAX_MEASUREMENT_BYTES = 1024 * 1024

#: Subjects are free-form labels (they need no registration), but must be
#: non-empty and reasonably short.
MAX_SUBJECT_LENGTH = 1024

#: |t| <= 10^18 keeps integer arithmetic well inside SQLite's 64 bit ints.
MAX_ABS_TIME = 10**18


def validate_identifier(value: Any, field: str = "identifier") -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise InvalidInput(f"{field} must be a non-empty ASCII alphanumeric string")
    if len(value) > MAX_IDENTIFIER_LENGTH:
        raise InvalidInput(f"{field} must be at most {MAX_IDENTIFIER_LENGTH} characters")
    return value


def validate_purpose(value: Any) -> str:
    return validate_identifier(value, "purpose")


def validate_subject(value: Any, field: str = "subject") -> str:
    """Subjects are opaque non-empty text (no prior registration required)."""
    if not isinstance(value, str) or not value:
        raise InvalidInput(f"{field} must be a non-empty string")
    if len(value) > MAX_SUBJECT_LENGTH:
        raise InvalidInput(f"{field} must be at most {MAX_SUBJECT_LENGTH} characters")
    return value


def validate_time(value: Any, field: str = "collected_at") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidInput(f"{field} must be an integer")
    if abs(value) > MAX_ABS_TIME:
        raise InvalidInput(f"{field} must satisfy |{field}| <= {MAX_ABS_TIME}")
    return value


def canonical_json(value: Any, identifier: str = "payload") -> str:
    """Return canonical JSON text for a measurement payload.

    Round-tripping through ``json.loads``/``json.dumps`` both proves the value
    is valid JSON and strips away formatting differences.
    """
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        decoded = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise InvalidInput(f"{identifier} is not a valid JSON value: {exc}") from exc
    # Re-dump the decoded copy so callers can rely on the canonical form.
    text = json.dumps(decoded, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(text.encode("utf-8")) > MAX_MEASUREMENT_BYTES:
        raise MeasurementTooLarge(identifier, MAX_MEASUREMENT_BYTES)
    return text


def decode_json(text: str) -> Any:
    """Decode a canonical JSON blob from the database."""
    return json.loads(text)



# ---------------------------------------------------------------------------
# Strict request-body parsers (used by the object input classes)
# ---------------------------------------------------------------------------


def require_object_body(body: Any) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise InvalidInput("request body must be a JSON object")
    return body


def require_field(body: dict[str, Any], key: str) -> Any:
    if key not in body:
        raise InvalidInput(f"missing required field {key!r}")
    return body[key]
