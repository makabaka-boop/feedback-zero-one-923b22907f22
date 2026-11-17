"""Application acceptance, eligibility evaluation and result persistence.

Eligibility rules (all evaluated *exclusively* at the version pinned at
acceptance time):

1.  Build the undirected graph whose nodes are subjects and whose edges are the
    associations visible at the pinned version.  The *member set* of a
    measurement is the identity connected component of its subject.  Edges are
    used once each; parallel edges and self loops are harmless.
2.  For a measurement with collection time ``t`` and the application purpose,
    an authorization is *applicable* when its subject is a member, its purpose
    matches and ``start_at <= t < end_at``.  (The interval is non-empty,
    left-closed and right-open.)
3.  If any applicable authorization denies, the measurement is excluded.
    Otherwise it is eligible only when at least one applicable authorization
    allows it.  No applicable authorization excludes it.
4.  The forward identity closure from authorized members inside an undirected
    component is the whole component, so the reported member set is always the
    complete component and the closure adds no further exclusion.

The result of an application is written to the database together with the
terminal status in one transaction; readers only ever see a waiting (no
result) or a completed (full result) / failed (reason) application.
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Sequence

from . import dataset, validation
from .dataset import Maintainer
from .errors import ApplicationConflict, UnknownApplication
from .store import Store

STATUS_WAITING = "waiting"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
TERMINAL = (STATUS_COMPLETED, STATUS_FAILED)


class BuildFailure(Exception):
    """A deterministic failure while building a result.

    Applications ending in this failure become terminally ``failed`` and are
    never rebuilt.
    """

    def __init__(self, reason: str, message: str | None = None) -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason

    def to_reason(self) -> dict[str, Any]:
        return {"code": self.reason, "message": self.message}


class TransientFailure(Exception):
    """A temporary failure; the application remains ``waiting`` and is retried."""

    def __init__(self, message: str = "temporary failure while building result") -> None:
        super().__init__(message)


#: ``fault_hook(stage, context)`` may raise BuildFailure/TransientFailure.
FaultHook = Callable[[str, dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[str, str] = {}
        self._rank: dict[str, int] = {}

    def add(self, node: str) -> None:
        if node not in self._parent:
            self._parent[node] = node
            self._rank[node] = 0

    def find(self, node: str) -> str:
        parent = self._parent
        root = node
        while parent[root] != root:
            root = parent[root]
        while parent[node] != root:
            parent[node], node = root, parent[node]
        return root

    def union(self, left: str, right: str) -> None:
        a, b = self.find(left), self.find(right)
        if a == b:
            return
        if self._rank[a] < self._rank[b]:
            a, b = b, a
        self._parent[b] = a
        if self._rank[a] == self._rank[b]:
            self._rank[a] += 1


class _Graph:
    """Identity graph: connected components over the *distinct* subjects.

    The graph is undirected, so the forward identity closure of a set of
    authorized subjects inside a component is the entire component.  Only the
    component members are needed by the rules; the directed closure can never
    shrink a component, which is exactly why every eligible record reports its
    full member set.
    """

    __slots__ = ("component_of", "members")

    def __init__(self, component_of: dict[str, str], members: dict[str, list[str]]) -> None:
        self.component_of = component_of
        self.members = members

    @classmethod
    def build(cls, edges: Sequence[tuple[str, str, str]]) -> "_Graph":
        uf = _UnionFind()
        for _identifier, subject_a, subject_b in edges:
            uf.add(subject_a)
            uf.add(subject_b)
            uf.union(subject_a, subject_b)
        members: dict[str, list[str]] = {}
        component_of: dict[str, str] = {}
        # ``_parent`` holds exactly the distinct subjects that appear in edges;
        # self loops and parallel edges add nothing after the deduplication.
        for node in uf._parent:
            root = uf.find(node)
            component_of[node] = root
            members.setdefault(root, []).append(node)
        for nodes in members.values():
            nodes.sort()
        return cls(component_of, members)

    def component_members(self, subject: str) -> list[str]:
        root = self.component_of.get(subject)
        if root is None:
            return []
        return self.members[root]

    def component_key(self, subject: str) -> str:
        """Component identity of a subject (isolated subjects are their own).

        ``"\x00"`` cannot occur in a subject through the public API (subjects
        are non-empty text but could contain NUL, so the prefix keeps keys of
        real subjects and isolated subjects apart).
        """
        return self.component_of.get(subject, "\x00isolated\x00" + subject)


class _IntervalIndex:
    """Answers interval stabbing queries for a fixed set of ``[start, end)``.

    Two structures are kept over the intervals sorted by start:

    * a running maximum of ends, which answers "does any interval contain
      ``t``?" in ``O(log n)``;
    * the intervals themselves, used to enumerate the covering ones (only
      needed for records that are actually eligible, where the identifier list
      is part of the required output).
    """

    __slots__ = ("_items", "_starts", "_prefix_max_end")

    def __init__(self, intervals: Sequence[tuple[int, int, dict[str, Any]]]) -> None:
        self._items = sorted(intervals, key=lambda item: (item[0], item[1]))
        self._starts = [item[0] for item in self._items]
        prefix: list[int] = []
        running: int | None = None
        for _start, end, _authorization in self._items:
            running = end if running is None else max(running, end)
            prefix.append(running)
        self._prefix_max_end = prefix

    def covers(self, moment: int) -> bool:
        """Whether at least one interval contains ``moment``."""
        position = bisect.bisect_right(self._starts, moment)
        return position > 0 and self._prefix_max_end[position - 1] > moment

    def covering(self, moment: int) -> list[dict[str, Any]]:
        """All authorizations whose interval contains ``moment``."""
        position = bisect.bisect_right(self._starts, moment)
        return [
            authorization
            for start, end, authorization in self._items[:position]
            if end > moment
        ]



# ---------------------------------------------------------------------------
# Result construction
# ---------------------------------------------------------------------------


def build_result(store: Store, version: int, purpose: str, fault_hook: FaultHook | None = None) -> dict[str, Any]:
    """Compute the eligibility result at ``version`` for ``purpose``.

    The function only reads (and does so inside the caller's serialised
    transaction when one is active, or from a consistent WAL snapshot
    otherwise), so it never observes a revision newer than ``version``.
    """
    def maybe_fault(stage: str, **context: Any) -> None:
        if fault_hook is not None:
            fault_hook(stage, context)

    maybe_fault("build_start", version=version, purpose=purpose)

    with store.read_snapshot():
        associations = dataset.associations_at(store, version)
        maybe_fault("build_associations", count=len(associations))
        graph = _Graph.build(associations)

        measurements = dataset.measurements_at(store, version)
        maybe_fault("build_measurements", count=len(measurements))

        authorizations = dataset.authorizations_at(store, version, purpose)
        maybe_fault("build_authorizations", count=len(authorizations))

    # Authorizations are grouped by identity component (a member's authorization
    # applies to every record whose subject shares that component) and split by
    # decision so both questions can be answered with an interval index.
    allow_intervals: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
    deny_intervals: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
    for authorization in authorizations:
        key = graph.component_key(authorization["subject"])
        target = deny_intervals if authorization["decision"] == "deny" else allow_intervals
        target.setdefault(key, []).append(
            (authorization["start_at"], authorization["end_at"], authorization)
        )
    allow_index = {key: _IntervalIndex(items) for key, items in allow_intervals.items()}
    deny_index = {key: _IntervalIndex(items) for key, items in deny_intervals.items()}

    records: list[dict[str, Any]] = []
    for measurement in measurements:
        subject = measurement["subject"]
        members = graph.component_members(subject)
        if not members:
            # Subject only exists through its measurement, no associations.
            members = [subject]
        collected_at = measurement["collected_at"]
        key = graph.component_key(subject)

        denies = deny_index.get(key)
        if denies is not None and denies.covers(collected_at):
            continue  # an explicit denial always excludes the record
        allows = allow_index.get(key)
        if allows is None or not allows.covers(collected_at):
            continue  # no applicable authorization also excludes the record

        # Each authorization is stored at most once per version, so the
        # identifiers returned by the covering query are already distinct.
        applicable_ids = sorted(
            authorization["identifier"] for authorization in allows.covering(collected_at)
        )
        records.append(
            {
                "measurement": dict(measurement),
                "members": list(members),
                "authorizations": applicable_ids,
            }
        )

    records.sort(key=lambda record: record["measurement"]["identifier"])
    maybe_fault("build_finish", records=len(records))
    return {
        "version": version,
        "purpose": purpose,
        "records": records,
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class EligibilityService:
    """High level facade combining maintenance, export and recovery."""

    def __init__(
        self,
        store: Store,
        *,
        fault_hook: FaultHook | None = None,
        auto_resume: bool = True,
    ) -> None:
        self.store = store
        self.maintainer = Maintainer(store)
        self.fault_hook = fault_hook
        self._process_lock = threading.Lock()
        #: Applications completed by the automatic resume performed on open.
        self.auto_resumed: list[str] = []
        if auto_resume:
            self.auto_resumed = self.resume_pending()

    # -- maintenance API ------------------------------------------------
    def add_measurements(self, batch: Sequence[Any]) -> int:
        return self.maintainer.add_measurements(batch)

    def add_associations(self, batch: Sequence[Any]) -> int:
        return self.maintainer.add_associations(batch)

    def add_authorizations(self, batch: Sequence[Any]) -> int:
        return self.maintainer.add_authorizations(batch)

    def replace_measurements(self, batch: Sequence[Any]) -> int:
        return self.maintainer.replace_measurements(batch)

    def replace_associations(self, batch: Sequence[Any]) -> int:
        return self.maintainer.replace_associations(batch)

    def replace_authorizations(self, batch: Sequence[Any]) -> int:
        return self.maintainer.replace_authorizations(batch)

    def replace_measurement(self, record: Any) -> int:
        return self.maintainer.replace_measurement(record)

    def replace_association(self, record: Any) -> int:
        return self.maintainer.replace_association(record)

    def replace_authorization(self, record: Any) -> int:
        return self.maintainer.replace_authorization(record)

    def delete_measurements(self, identifiers: Sequence[Any]) -> int:
        return self.maintainer.delete_measurements(identifiers)

    def delete_associations(self, identifiers: Sequence[Any]) -> int:
        return self.maintainer.delete_associations(identifiers)

    def delete_authorizations(self, identifiers: Sequence[Any]) -> int:
        return self.maintainer.delete_authorizations(identifiers)

    def current_version(self) -> int:
        return self.store.current_version()

    # -- application API ------------------------------------------------
    def accept(self, purpose: Any, application_id: Any = None) -> dict[str, Any]:
        """Register an export application and answer with its current state.

        Registration is committed *before* any result is built, so a retry
        (same identifier, same purpose) always finds the original application.
        """
        purpose = validation.validate_purpose(purpose)
        if application_id is None:
            app_id = uuid.uuid4().hex
        else:
            app_id = validation.validate_identifier(application_id, "application identifier")

        with self.store.transaction() as session:
            row = session.query_one(
                "SELECT * FROM applications WHERE application_id = ?", (app_id,)
            )
            if row is not None:
                if row["purpose"] != purpose:
                    raise ApplicationConflict(app_id, row["purpose"], purpose)
            else:
                now = time.time()
                version = session.current_version()
                session.execute(
                    "INSERT INTO applications"
                    " (application_id, purpose, accepted_version, status,"
                    "  reason_json, result_json, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
                    (app_id, purpose, version, STATUS_WAITING, now, now),
                )

        # A fault injected at this point simulates a crash after the durable
        # registration; the application must be recoverable later.
        self._maybe_fault("accept_after_persist", application_id=app_id)

        # Best effort synchronous completion.  For a retry this also resumes a
        # previously transiently failed attempt.
        self._process_if_waiting(app_id, swallow_transient=True)
        return self.get_application(app_id)

    def get_application(self, application_id: Any, purpose: Any = None) -> dict[str, Any]:
        app_id = validation.validate_identifier(application_id, "application identifier")
        row = self.store.query_one(
            "SELECT * FROM applications WHERE application_id = ?", (app_id,)
        )
        if row is None:
            raise UnknownApplication(app_id)
        if purpose is not None:
            requested = validation.validate_purpose(purpose)
            if requested != row["purpose"]:
                raise ApplicationConflict(app_id, row["purpose"], requested)
        return self._serialize_application(row)

    def resume(self, application_id: Any) -> dict[str, Any]:
        app_id = validation.validate_identifier(application_id, "application identifier")
        self._process_if_waiting(app_id, swallow_transient=False)
        return self.get_application(app_id)

    def resume_pending(self) -> list[str]:
        """Process every waiting application (used at startup / after faults)."""
        rows = self.store.query_all(
            "SELECT application_id FROM applications WHERE status = ? ORDER BY created_at, application_id",
            (STATUS_WAITING,),
        )
        recovered: list[str] = []
        for row in rows:
            app_id = row["application_id"]
            try:
                if self._process_if_waiting(app_id, swallow_transient=False):
                    recovered.append(app_id)
            except TransientFailure:
                continue
        return recovered

    # -- internals ------------------------------------------------------
    def _maybe_fault(self, stage: str, **context: Any) -> None:
        if self.fault_hook is not None:
            self.fault_hook(stage, context)

    def _serialize_application(self, row: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "application_id": row["application_id"],
            "purpose": row["purpose"],
            "accepted_version": int(row["accepted_version"]),
            "status": row["status"],
        }
        if row["status"] == STATUS_COMPLETED:
            result["result"] = json.loads(row["result_json"])
        elif row["status"] == STATUS_FAILED:
            result["reason"] = json.loads(row["reason_json"])
        return result

    def _process_if_waiting(self, app_id: str, *, swallow_transient: bool) -> bool:
        """Build and persist the result if (and only if) still waiting.

        Returns True when this call completed the application.

        The heavy build runs *outside* the write transaction: the version is
        pinned and committed revisions are immutable, so the snapshot cannot
        change while building.  Only the short status check and the persist
        happen under the write lock, and the status is re-checked there, so a
        concurrent processor cannot complete the application twice.
        """
        with self._process_lock:
            try:
                row = self.store.query_one(
                    "SELECT * FROM applications WHERE application_id = ?", (app_id,)
                )
                if row is None:
                    raise UnknownApplication(app_id)
                if row["status"] != STATUS_WAITING:
                    return False
                version = int(row["accepted_version"])
                purpose = row["purpose"]
                try:
                    result = build_result(self.store, version, purpose, self.fault_hook)
                except TransientFailure:
                    if swallow_transient:
                        return False
                    raise
                except (ValueError, KeyError, TypeError) as exc:
                    # Stored data cannot be interpreted (e.g. a payload that is
                    # not decodable).  This is deterministic for a pinned
                    # version, so the application ends terminally failed.
                    raise BuildFailure("invalid_stored_data", str(exc)) from exc
                except sqlite3.OperationalError as exc:
                    # Locking / I/O pressure is assumed transient: stay waiting.
                    if swallow_transient:
                        return False
                    raise TransientFailure(f"temporary database error: {exc}") from exc
                except sqlite3.DatabaseError as exc:
                    raise BuildFailure("store_corrupt", str(exc)) from exc
                result_json = json.dumps(result, sort_keys=True, ensure_ascii=False)
                with self.store.transaction() as session:
                    current = session.query_one(
                        "SELECT status FROM applications WHERE application_id = ?", (app_id,)
                    )
                    if current is None:
                        raise UnknownApplication(app_id)
                    if current["status"] != STATUS_WAITING:
                        # Another processor won the race; its result stands.
                        return False
                    session.execute(
                        "UPDATE applications SET status = ?, result_json = ?,"
                        " reason_json = NULL, updated_at = ? WHERE application_id = ?",
                        (STATUS_COMPLETED, result_json, time.time(), app_id),
                    )
                    return True
            except BuildFailure as failure:
                # Deterministic build failure -> terminal failure, persisted
                # atomically.  The reason is recorded exactly once.
                reason_json = json.dumps(failure.to_reason(), sort_keys=True)
                with self.store.transaction() as session:
                    current = session.query_one(
                        "SELECT status FROM applications WHERE application_id = ?", (app_id,)
                    )
                    if current is None:
                        raise UnknownApplication(app_id)
                    if current["status"] != STATUS_WAITING:
                        return False
                    session.execute(
                        "UPDATE applications SET status = ?, reason_json = ?,"
                        " result_json = NULL, updated_at = ? WHERE application_id = ?",
                        (STATUS_FAILED, reason_json, time.time(), app_id),
                    )
                    return False
            except TransientFailure:
                if swallow_transient:
                    return False
                raise
