"""Implementation of the business rules on top of :mod:`store`.

The database models three kinds of *objects*:

* measurements  -- stable identifier, subject, integer collection timestamp and
  an arbitrary JSON payload;
* associations  -- an undirected edge with its own identifier and two subjects
  (self loops and parallel edges are allowed, subjects need no separate
  registration);
* authorizations -- identifier, subject, purpose, allow/deny and a non-empty
  left-closed right-open integer time interval.

Identifiers are unique among the currently existing objects *of the same
kind* (a measurement and an association may share a spelling; they are
distinct objects).  A successful revision closes the previous rows
of every touched identifier and opens a new row at the new version; untouched
rows simply keep their current version and therefore remain visible.
Version visibility at version ``V`` is exactly ``valid_from <= V < valid_to``.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from . import validation
from .errors import (
    DuplicateIdentifier,
    InvalidInput,
    UnknownObject,
)
from .store import Store, WriteSession

MEASUREMENT = "measurement"
ASSOCIATION = "association"
AUTHORIZATION = "authorization"

# ---------------------------------------------------------------------------
# Validated input records
# ---------------------------------------------------------------------------


class MeasurementInput:
    __slots__ = ("identifier", "subject", "collected_at", "payload")

    def __init__(self, identifier: str, subject: str, collected_at: int, payload: str) -> None:
        self.identifier = identifier
        self.subject = subject
        self.collected_at = collected_at
        self.payload = payload

    @classmethod
    def parse(cls, raw: Any) -> "MeasurementInput":
        from .validation import require_object_body, require_field

        body = require_object_body(raw)
        ident = validation.validate_identifier(require_field(body, "identifier"), "measurement identifier")
        subject = validation.validate_subject(require_field(body, "subject"))
        collected_at = validation.validate_time(require_field(body, "collected_at"), "collected_at")
        payload = validation.canonical_json(require_field(body, "payload"), ident)
        return cls(ident, subject, collected_at, payload)


class AssociationInput:
    __slots__ = ("identifier", "subject_a", "subject_b")

    def __init__(self, identifier: str, subject_a: str, subject_b: str) -> None:
        self.identifier = identifier
        self.subject_a = subject_a
        self.subject_b = subject_b

    @classmethod
    def parse(cls, raw: Any) -> "AssociationInput":
        from .validation import require_object_body, require_field

        body = require_object_body(raw)
        ident = validation.validate_identifier(require_field(body, "identifier"), "association identifier")
        a = validation.validate_subject(require_field(body, "subject_a"), "subject_a")
        b = validation.validate_subject(require_field(body, "subject_b"), "subject_b")
        return cls(ident, a, b)


class AuthorizationInput:
    __slots__ = ("identifier", "subject", "purpose", "effect", "start_at", "end_at")

    def __init__(
        self,
        identifier: str,
        subject: str,
        purpose: str,
        effect: int,
        start_at: int,
        end_at: int,
    ) -> None:
        self.identifier = identifier
        self.subject = subject
        self.purpose = purpose
        self.effect = effect
        self.start_at = start_at
        self.end_at = end_at

    @classmethod
    def parse(cls, raw: Any) -> "AuthorizationInput":
        from .validation import require_object_body, require_field

        body = require_object_body(raw)
        ident = validation.validate_identifier(require_field(body, "identifier"), "authorization identifier")
        subject = validation.validate_subject(require_field(body, "subject"))
        purpose = validation.validate_purpose(require_field(body, "purpose"))
        decision = require_field(body, "decision")
        if not isinstance(decision, str) or decision not in ("allow", "deny"):
            raise InvalidInput("decision must be the string 'allow' or 'deny'")
        start_at = validation.validate_time(require_field(body, "start_at"), "start_at")
        end_at = validation.validate_time(require_field(body, "end_at"), "end_at")
        if not start_at < end_at:
            raise InvalidInput("authorization interval must be non-empty (start_at < end_at)")
        return cls(ident, subject, purpose, 1 if decision == "allow" else 0, start_at, end_at)


# ---------------------------------------------------------------------------
# Data maintenance operations (each opens its own serialised revision)
# ---------------------------------------------------------------------------


class Maintainer:
    def __init__(self, store: Store) -> None:
        self.store = store

    # -- generic helpers -------------------------------------------------
    def _assert_batch_unique(self, inputs: Sequence[Any], kind: str) -> None:
        if not inputs:
            raise InvalidInput(f"{kind} batch must not be empty")
        seen: set[str] = set()
        for item in inputs:
            if item.identifier in seen:
                raise DuplicateIdentifier(
                    item.identifier, f"duplicate {kind} identifier within batch"
                )
            seen.add(item.identifier)

    def _register_new(self, session: WriteSession, kind: str, identifiers: Iterable[str], version: int) -> None:
        for ident in identifiers:
            row = session.query_one(
                "SELECT 1 AS ok FROM objects WHERE kind = ? AND identifier = ?", (kind, ident)
            )
            if row is not None:
                raise DuplicateIdentifier(ident, f"{kind} {ident!r} already exists")
            session.execute(
                "INSERT INTO objects (kind, identifier, current_version) VALUES (?, ?, ?)",
                (kind, ident, version),
            )

    def _replace(self, session: WriteSession, kind: str, identifiers: Iterable[str], version: int) -> None:
        for ident in identifiers:
            row = session.query_one(
                "SELECT current_version FROM objects WHERE kind = ? AND identifier = ?",
                (kind, ident),
            )
            if row is None:
                raise UnknownObject(kind, ident)
            session.execute(
                "UPDATE objects SET current_version = ? WHERE kind = ? AND identifier = ?",
                (version, kind, ident),
            )

    def _delete(self, session: WriteSession, kind: str, identifiers: Iterable[str]) -> None:
        for ident in identifiers:
            row = session.query_one(
                "SELECT 1 AS ok FROM objects WHERE kind = ? AND identifier = ?", (kind, ident)
            )
            if row is None:
                raise UnknownObject(kind, ident)
            session.execute(
                "DELETE FROM objects WHERE kind = ? AND identifier = ?", (kind, ident)
            )

    def _close(self, session: WriteSession, table: str, identifiers: Sequence[str], version: int) -> None:
        for ident in identifiers:
            session.execute(
                f"UPDATE {table} SET valid_to = ? WHERE identifier = ? AND valid_to IS NULL",
                (version, ident),
            )

    # -- public operations (plain adds) ----------------------------------
    def add_measurements(self, raw: Sequence[Any]) -> int:
        inputs = [MeasurementInput.parse(item) for item in raw]
        self._assert_batch_unique(inputs, MEASUREMENT)
        with self.store.revision() as session:
            self._register_new(session, MEASUREMENT, (m.identifier for m in inputs), session.version)
            for m in inputs:
                session.execute(
                    "INSERT INTO measurements"
                    " (identifier, valid_from, valid_to, subject, collected_at, payload)"
                    " VALUES (?, ?, NULL, ?, ?, ?)",
                    (m.identifier, session.version, m.subject, m.collected_at, m.payload),
                )
            return session.version

    def add_associations(self, raw: Sequence[Any]) -> int:
        inputs = [AssociationInput.parse(item) for item in raw]
        self._assert_batch_unique(inputs, ASSOCIATION)
        with self.store.revision() as session:
            self._register_new(session, ASSOCIATION, (a.identifier for a in inputs), session.version)
            for a in inputs:
                session.execute(
                    "INSERT INTO associations"
                    " (identifier, valid_from, valid_to, subject_a, subject_b)"
                    " VALUES (?, ?, NULL, ?, ?)",
                    (a.identifier, session.version, a.subject_a, a.subject_b),
                )
            return session.version

    def add_authorizations(self, raw: Sequence[Any]) -> int:
        inputs = [AuthorizationInput.parse(item) for item in raw]
        self._assert_batch_unique(inputs, AUTHORIZATION)
        with self.store.revision() as session:
            self._register_new(session, AUTHORIZATION, (a.identifier for a in inputs), session.version)
            for a in inputs:
                session.execute(
                    "INSERT INTO authorizations"
                    " (identifier, valid_from, valid_to, subject, purpose, effect, start_at, end_at)"
                    " VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                    (a.identifier, session.version, a.subject, a.purpose, a.effect, a.start_at, a.end_at),
                )
            return session.version

    # -- public operations (whole-object replacement) --------------------
    #
    # Replacement acts on a batch in one revision.  Every identifier must name
    # an existing object of the same kind; nothing is changed unless the whole
    # batch is valid (validation happens before the transaction opens, and any
    # database error rolls it back).
    def replace_measurements(self, raw: Sequence[Any]) -> int:
        inputs = [MeasurementInput.parse(item) for item in raw]
        self._assert_batch_unique(inputs, MEASUREMENT)
        with self.store.revision() as session:
            self._replace(session, MEASUREMENT, (m.identifier for m in inputs), session.version)
            self._close(session, "measurements", [m.identifier for m in inputs], session.version)
            for m in inputs:
                session.execute(
                    "INSERT INTO measurements"
                    " (identifier, valid_from, valid_to, subject, collected_at, payload)"
                    " VALUES (?, ?, NULL, ?, ?, ?)",
                    (m.identifier, session.version, m.subject, m.collected_at, m.payload),
                )
            return session.version

    def replace_associations(self, raw: Sequence[Any]) -> int:
        inputs = [AssociationInput.parse(item) for item in raw]
        self._assert_batch_unique(inputs, ASSOCIATION)
        with self.store.revision() as session:
            self._replace(session, ASSOCIATION, (a.identifier for a in inputs), session.version)
            self._close(session, "associations", [a.identifier for a in inputs], session.version)
            for a in inputs:
                session.execute(
                    "INSERT INTO associations"
                    " (identifier, valid_from, valid_to, subject_a, subject_b)"
                    " VALUES (?, ?, NULL, ?, ?)",
                    (a.identifier, session.version, a.subject_a, a.subject_b),
                )
            return session.version

    def replace_authorizations(self, raw: Sequence[Any]) -> int:
        inputs = [AuthorizationInput.parse(item) for item in raw]
        self._assert_batch_unique(inputs, AUTHORIZATION)
        with self.store.revision() as session:
            self._replace(session, AUTHORIZATION, (a.identifier for a in inputs), session.version)
            self._close(session, "authorizations", [a.identifier for a in inputs], session.version)
            for a in inputs:
                session.execute(
                    "INSERT INTO authorizations"
                    " (identifier, valid_from, valid_to, subject, purpose, effect, start_at, end_at)"
                    " VALUES (?, ?, NULL, ?, ?, ?, ?, ?)",
                    (a.identifier, session.version, a.subject, a.purpose, a.effect, a.start_at, a.end_at),
                )
            return session.version

    # Convenience single-object wrappers.
    def replace_measurement(self, raw: Any) -> int:
        return self.replace_measurements([raw])

    def replace_association(self, raw: Any) -> int:
        return self.replace_associations([raw])

    def replace_authorization(self, raw: Any) -> int:
        return self.replace_authorizations([raw])

    # -- public operations (deletion) ------------------------------------
    def delete_measurements(self, raw_identifiers: Sequence[Any]) -> int:
        identifiers = [validation.validate_identifier(i, "measurement identifier") for i in raw_identifiers]
        self._assert_batch_unique([SimpleId(i) for i in identifiers], MEASUREMENT)
        with self.store.revision() as session:
            self._delete(session, MEASUREMENT, identifiers)
            self._close(session, "measurements", identifiers, session.version)
            return session.version

    def delete_associations(self, raw_identifiers: Sequence[Any]) -> int:
        identifiers = [validation.validate_identifier(i, "association identifier") for i in raw_identifiers]
        self._assert_batch_unique([SimpleId(i) for i in identifiers], ASSOCIATION)
        with self.store.revision() as session:
            self._delete(session, ASSOCIATION, identifiers)
            self._close(session, "associations", identifiers, session.version)
            return session.version

    def delete_authorizations(self, raw_identifiers: Sequence[Any]) -> int:
        identifiers = [validation.validate_identifier(i, "authorization identifier") for i in raw_identifiers]
        self._assert_batch_unique([SimpleId(i) for i in identifiers], AUTHORIZATION)
        with self.store.revision() as session:
            self._delete(session, AUTHORIZATION, identifiers)
            self._close(session, "authorizations", identifiers, session.version)
            return session.version


class SimpleId:
    __slots__ = ("identifier",)

    def __init__(self, identifier: str) -> None:
        self.identifier = identifier


# ---------------------------------------------------------------------------
# Versioned snapshot reads
# ---------------------------------------------------------------------------


def version_exists(store: Store, version: int) -> bool:
    return store.query_one("SELECT 1 AS ok FROM versions WHERE id = ?", (version,)) is not None


def measurements_at(store: Store, version: int) -> list[dict[str, Any]]:
    """Measurements visible at ``version``, ordered by identifier (ASCII)."""
    rows = store.query_all(
        "SELECT identifier, subject, collected_at, payload FROM measurements"
        " WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
        " ORDER BY identifier",
        (version, version),
    )
    return [
        {
            "identifier": r["identifier"],
            "subject": r["subject"],
            "collected_at": int(r["collected_at"]),
            "payload": validation.decode_json(r["payload"]),
        }
        for r in rows
    ]


def associations_at(store: Store, version: int) -> list[tuple[str, str, str]]:
    rows = store.query_all(
        "SELECT identifier, subject_a, subject_b FROM associations"
        " WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
        " ORDER BY identifier",
        (version, version),
    )
    return [(r["identifier"], r["subject_a"], r["subject_b"]) for r in rows]


def authorizations_at(store: Store, version: int, purpose: str) -> list[dict[str, Any]]:
    rows = store.query_all(
        "SELECT identifier, subject, purpose, effect, start_at, end_at FROM authorizations"
        " WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) AND purpose = ?"
        " ORDER BY identifier",
        (version, version, purpose),
    )
    return [_authorization_row(r) for r in rows]


def all_authorizations_at(store: Store, version: int) -> list[dict[str, Any]]:
    rows = store.query_all(
        "SELECT identifier, subject, purpose, effect, start_at, end_at FROM authorizations"
        " WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)"
        " ORDER BY identifier",
        (version, version),
    )
    return [_authorization_row(r) for r in rows]


def _authorization_row(row: Any) -> dict[str, Any]:
    return {
        "identifier": row["identifier"],
        "subject": row["subject"],
        "purpose": row["purpose"],
        "decision": "allow" if int(row["effect"]) == 1 else "deny",
        "start_at": int(row["start_at"]),
        "end_at": int(row["end_at"]),
    }
