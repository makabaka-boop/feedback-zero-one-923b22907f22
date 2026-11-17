"""Acceptance tests for the eligibility service (business rules)."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eligibility_service import (  # noqa: E402
    ApplicationConflict,
    BuildFailure,
    DuplicateIdentifier,
    EligibilityService,
    InvalidInput,
    Store,
    TransientFailure,
    UnknownApplication,
    UnknownObject,
)


def measurement(ident, subject, at=100, payload=None):
    return {
        "identifier": ident,
        "subject": subject,
        "collected_at": at,
        "payload": payload if payload is not None else {"id": ident},
    }


def association(ident, a, b):
    return {"identifier": ident, "subject_a": a, "subject_b": b}


def auth(ident, subject, decision="allow", start=0, end=1000, purpose="p1"):
    return {
        "identifier": ident,
        "subject": subject,
        "purpose": purpose,
        "decision": decision,
        "start_at": start,
        "end_at": end,
    }


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.sqlite")
        self.store = Store(self.db)
        self.service = EligibilityService(self.store, auto_resume=False)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # helpers -----------------------------------------------------------
    def reopen(self, hook=None, auto_resume=False):
        self.store.close()
        self.store = Store(self.db)
        self.service = EligibilityService(self.store, fault_hook=hook, auto_resume=auto_resume)
        return self.service

    def export(self, purpose="p1", app_id=None):
        return self.service.accept(purpose, app_id)

    def ids(self, response):
        self.assertEqual(response["status"], "completed", response)
        return [record["measurement"]["identifier"] for record in response["result"]["records"]]


class EligibilityRuleTests(ServiceTestCase):
    def test_allow_requires_member_and_interval(self):
        self.service.add_measurements([measurement("m1", "alice", 100)])
        self.service.add_associations([association("e1", "alice", "bob")])
        self.service.add_authorizations(
            [
                # right-open interval: 100 is outside [0, 100)
                auth("aEarly", "bob", start=0, end=100),
                # applies at t = 100
                auth("aExact", "bob", start=100, end=200),
            ]
        )
        response = self.export()
        self.assertEqual(self.ids(response), ["m1"])
        record = response["result"]["records"][0]
        self.assertEqual(record["members"], ["alice", "bob"])
        self.assertEqual(record["authorizations"], ["aExact"])
        self.assertEqual(record["measurement"]["payload"], {"id": "m1"})

    def test_deny_anywhere_in_component_excludes(self):
        self.service.add_measurements([measurement("m1", "alice", 100)])
        self.service.add_associations(
            [association("e1", "alice", "bob"), association("e2", "bob", "carol")]
        )
        self.service.add_authorizations(
            [
                auth("aAllow", "alice"),
                auth("aDeny", "carol", decision="deny"),
            ]
        )
        self.assertEqual(self.ids(self.export()), [])

    def test_deny_without_overlapping_interval_is_ignored(self):
        self.service.add_measurements([measurement("m1", "alice", 100)])
        self.service.add_associations([association("e1", "alice", "bob")])
        self.service.add_authorizations(
            [
                auth("aAllow", "alice"),
                auth("aDenyfuture", "bob", decision="deny", start=500, end=600),
            ]
        )
        response = self.export()
        self.assertEqual(self.ids(response), ["m1"])
        self.assertEqual(response["result"]["records"][0]["authorizations"], ["aAllow"])

    def test_no_authorization_excludes_and_unknown_subject_never_matches(self):
        self.service.add_measurements(
            [
                measurement("m1", "alice"),
                measurement("mGhost", "ghost"),
            ]
        )
        self.service.add_associations([association("e1", "alice", "bob")])
        self.service.add_authorizations([auth("a1", "bob"), auth("a2", "nobody")])
        response = self.export()
        # ghost is not in bob's component -> no authorization -> excluded.
        self.assertEqual(self.ids(response), ["m1"])
        self.assertEqual(response["result"]["records"][0]["members"], ["alice", "bob"])

    def test_purpose_isolation(self):
        self.service.add_measurements([measurement("m1", "alice")])
        self.service.add_authorizations(
            [
                auth("p1Allow", "alice", purpose="p1"),
                auth("p2Deny", "alice", decision="deny", purpose="p2"),
            ]
        )
        self.assertEqual(self.ids(self.export("p1")), ["m1"])
        self.assertEqual(self.ids(self.export("p2")), [])
        self.assertEqual(self.ids(self.export("p3")), [])

    def test_full_member_set_and_all_applicable_authorizations(self):
        # Component {alice, bob, carol, dave} via parallel edges and self loop.
        self.service.add_measurements([measurement("m1", "alice", 50)])
        self.service.add_associations(
            [
                association("e1", "alice", "bob"),
                association("e2", "bob", "alice"),  # parallel edge, reversed
                association("e3", "bob", "bob"),  # self loop
                association("e4", "bob", "carol"),
                association("e5", "carol", "dave"),
            ]
        )
        self.service.add_authorizations(
            [
                auth("aDave", "dave", start=0, end=60),
                auth("aCarol", "carol", start=10, end=60),
                auth("aCarolExpired", "carol", start=0, end=50),  # t=50 is excluded
                auth("aBobWrongPurpose", "bob", purpose="p2"),
            ]
        )
        response = self.export()
        self.assertEqual(self.ids(response), ["m1"])
        record = response["result"]["records"][0]
        self.assertEqual(record["members"], ["alice", "bob", "carol", "dave"])
        self.assertEqual(record["authorizations"], ["aCarol", "aDave"])

    def test_identical_records_are_not_merged(self):
        self.service.add_measurements(
            [
                measurement("m2", "alice", 10, {"same": True}),
                measurement("m1", "alice", 10, {"same": True}),
            ]
        )
        self.service.add_authorizations([auth("a1", "alice")])
        response = self.export()
        self.assertEqual(self.ids(response), ["m1", "m2"])
        self.assertEqual(len(response["result"]["records"]), 2)

    def test_records_and_members_use_ascii_order(self):
        self.service.add_measurements(
            [
                measurement("mC", "subjB"),
                measurement("mB", "subjA"),
                measurement("mAA", "subj10"),
                measurement("mAB", "subj2"),
            ]
        )
        self.service.add_associations(
            [
                association("e1", "subjA", "zed"),
                association("e2", "subjA", "Alpha"),
                association("e3", "subjA", "alpha"),
            ]
        )
        self.service.add_authorizations(
            [
                auth("a1", "alpha", start=0, end=1000),
                auth("a2", "subj10", start=0, end=1000),
                auth("a3", "subj2", start=0, end=1000),
                auth("a4", "subjB", start=0, end=1000),
            ]
        )
        response = self.export()
        # ASCII order: "mAA" < "mAB" < "mB" < "mC" (digits sort before letters).
        self.assertEqual(self.ids(response), ["mAA", "mAB", "mB", "mC"])
        members = response["result"]["records"][0]["members"]  # mAA -> subj10 only
        self.assertEqual(members, ["subj10"])
        record_for_mB = response["result"]["records"][2]
        self.assertEqual(record_for_mB["measurement"]["subject"], "subjA")
        self.assertEqual(record_for_mB["members"], ["Alpha", "alpha", "subjA", "zed"])

    def test_empty_result_is_completed(self):
        response = self.export("p1", "emptyApp")
        self.assertEqual(response["status"], "completed")
        self.assertEqual(response["result"]["records"], [])
        self.assertEqual(response["result"]["version"], 1)

    def test_complete_result_contains_full_original_data(self):
        payload = {"nested": {"x": [1, 2, {"y": "héllo"}]}, "flag": None}
        self.service.add_measurements([measurement("m1", "alice", -5, payload)])
        self.service.add_authorizations([auth("a1", "alice", start=-100, end=-4)])
        response = self.export()
        record = response["result"]["records"][0]
        self.assertEqual(record["measurement"], {
            "identifier": "m1",
            "subject": "alice",
            "collected_at": -5,
            "payload": payload,
        })


class MaintenanceTests(ServiceTestCase):
    def test_add_returns_version_and_rejects_bad_batches_atomically(self):
        self.assertEqual(self.service.current_version(), 1)
        v = self.service.add_measurements([measurement("m1", "s1"), measurement("m2", "s2")])
        self.assertEqual(v, 2)
        self.assertEqual(self.service.current_version(), 2)

        with self.assertRaises(DuplicateIdentifier):
            self.service.add_measurements([measurement("m3", "s3"), measurement("m3", "s4")])
        self.assertEqual(self.service.current_version(), 2)

        with self.assertRaises(InvalidInput):
            self.service.add_measurements([measurement("m4", "s4"), measurement("!", "s5")])
        self.assertEqual(self.service.current_version(), 2)

        # Second record of a failing batch must not have been created either.
        with self.assertRaises(DuplicateIdentifier):
            self.service.add_measurements([measurement("m5", "s5"), measurement("m1", "s6")])
        self.assertEqual(self.service.current_version(), 2)
        self.service.add_measurements([measurement("m5", "s5")])
        self.assertEqual(self.service.current_version(), 3)

    def test_same_identifier_may_exist_in_different_namespaces(self):
        self.service.add_measurements([measurement("x1", "s1")])
        self.service.add_associations([association("x1", "a", "b")])
        self.service.add_authorizations([auth("x1", "a")])
        self.assertEqual(self.service.current_version(), 4)
        # Within a kind duplicates are still rejected.
        with self.assertRaises(DuplicateIdentifier):
            self.service.add_associations([association("x1", "c", "d")])
        # Deleting one kind leaves the others intact.
        self.service.delete_measurements(["x1"])
        response = self.export("p1", "appNs")
        self.assertEqual(response["result"]["records"], [])
        self.assertEqual(response["status"], "completed")
        self.assertEqual(self.service.current_version(), 5)

    def test_replace_and_delete_semantics(self):
        self.service.add_measurements([measurement("m1", "s1", 10)])
        self.service.add_authorizations([auth("a1", "s1")])
        version = self.service.replace_measurement(measurement("m1", "s1", 20, {"new": 1}))
        self.assertEqual(version, 4)
        response = self.export("p1", "appAfterReplace")
        self.assertEqual(response["accepted_version"], 4)
        self.assertEqual(response["result"]["records"][0]["measurement"]["collected_at"], 20)

        with self.assertRaises(UnknownObject):
            self.service.replace_measurement(measurement("mMissing", "s1"))
        # a1 exists only as an authorization, not as an association/measurement.
        with self.assertRaises(UnknownObject):
            self.service.delete_associations(["a1"])
        with self.assertRaises(UnknownObject):
            self.service.delete_measurements(["a1"])
        self.assertEqual(self.service.current_version(), 4)

        self.service.delete_measurements(["m1"])
        self.assertEqual(self.service.current_version(), 5)
        with self.assertRaises(UnknownObject):
            self.service.delete_measurements(["m1"])

    def test_rejected_inputs_never_advance_version(self):
        bad_batches = [
            ([],),
            ([{"identifier": "bad id", "subject": "s", "collected_at": 1, "payload": 1}],),
            ([{"identifier": "m1", "subject": "", "collected_at": 1, "payload": 1}],),
            ([{"identifier": "m1", "subject": "s", "collected_at": 1.5, "payload": 1}],),
            ([{"identifier": "m1", "subject": "s", "collected_at": True, "payload": 1}],),
            ([{"identifier": "m1", "subject": "s", "collected_at": 1}],),
            ([{"identifier": "m1", "subject": "s", "collected_at": 1, "payload": float("nan")}],),
        ]
        for args in bad_batches:
            with self.assertRaises(InvalidInput):
                self.service.add_measurements(*args)
            self.assertEqual(self.service.current_version(), 1)

        bad_auths = [
            [auth("a1", "s", start=5, end=5)],
            [auth("a1", "s", start=6, end=5)],
            [dict(auth("a1", "s"), decision="maybe")],
            [dict(auth("a1", "s"), purpose="bad purpose")],
            [dict(auth("a1", "s"), start_at="1")],
        ]
        for batch in bad_auths:
            with self.assertRaises(InvalidInput):
                self.service.add_authorizations(batch)
            self.assertEqual(self.service.current_version(), 1)

        with self.assertRaises(InvalidInput):
            self.service.accept("bad purpose")
        with self.assertRaises(InvalidInput):
            self.service.accept("p1", "not-an-id!")


class VersionIsolationTests(ServiceTestCase):
    def test_later_revisions_do_not_change_old_application(self):
        self.service.add_measurements([measurement("m1", "alice", 100)])
        self.service.add_associations([association("e1", "alice", "bob")])
        self.service.add_authorizations([auth("a1", "bob")])
        first = self.export("p1", "app1")
        self.assertEqual(self.ids(first), ["m1"])

        # Revise everything that could change the result.
        self.service.replace_authorization(auth("a1", "bob", decision="deny"))
        self.service.replace_association(association("e1", "alice", "carol"))
        self.service.replace_measurement(measurement("m1", "alice", 200, {"changed": True}))
        self.service.add_measurements([measurement("mAB", "alice", 100)])
        self.service.delete_associations(["e1"])

        later = self.export("p1", "app2")
        self.assertGreater(later["accepted_version"], first["accepted_version"])
        self.assertEqual(self.ids(later), [])

        # Retrying the original application still returns the frozen result.
        retried = self.export("p1", "app1")
        self.assertEqual(retried, first)
        queried = self.service.get_application("app1", "p1")
        self.assertEqual(queried, first)

    def test_old_version_snapshot_reconstructs_deleted_objects(self):
        self.service.add_measurements([measurement("m1", "s1", 1)])
        self.service.add_authorizations([auth("a1", "s1")])
        pinned = self.service.current_version()
        self.service.delete_measurements(["m1"])
        self.service.delete_authorizations(["a1"])
        self.assertEqual(self.service.current_version(), pinned + 2)

        from eligibility_service import build_result

        frozen = build_result(self.store, pinned, "p1")
        self.assertEqual(
            [r["measurement"]["identifier"] for r in frozen["records"]], ["m1"]
        )
        current = build_result(self.store, self.service.current_version(), "p1")
        self.assertEqual(current["records"], [])


class ApplicationLifecycleTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.service.add_measurements([measurement("m1", "alice", 10)])
        self.service.add_authorizations([auth("a1", "alice")])

    def test_retry_same_purpose_returns_current_state(self):
        first = self.export("p1", "app1")
        self.assertEqual(first["status"], "completed")
        again = self.export("p1", "app1")
        self.assertEqual(again, first)
        self.assertIsNotNone(again.get("result"))

    def test_retry_different_purpose_is_rejected(self):
        self.export("p1", "app1")
        with self.assertRaises(ApplicationConflict):
            self.export("p2", "app1")
        with self.assertRaises(ApplicationConflict):
            self.service.get_application("app1", "p2")
        self.assertEqual(self.service.get_application("app1")["purpose"], "p1")

    def test_unknown_application_is_rejected(self):
        with self.assertRaises(UnknownApplication):
            self.service.get_application("nope")
        with self.assertRaises(UnknownApplication):
            self.service.resume("nope")

    def test_transient_fault_leaves_waiting_then_retry_completes(self):
        calls = {"n": 0}

        def hook(stage, context):
            if stage == "build_measurements" and calls["n"] == 0:
                calls["n"] += 1
                raise TransientFailure("boom")

        service = self.reopen(hook=hook)
        response = service.accept("p1", "app1")
        self.assertEqual(response["status"], "waiting")
        self.assertNotIn("result", response)
        self.assertNotIn("reason", response)

        # Transient failure is not terminal: a retry completes the application.
        calls["n"] = 99
        retried = service.accept("p1", "app1")
        self.assertEqual(retried["status"], "completed")
        self.assertEqual(
            [r["measurement"]["identifier"] for r in retried["result"]["records"]], ["m1"]
        )

    def test_crash_after_accept_is_recovered_after_restart(self):
        fault = {"raise": True}

        def hook(stage, context):
            if stage == "accept_after_persist" and fault["raise"]:
                raise TransientFailure("simulated crash")

        service = self.reopen(hook=hook)
        with self.assertRaises(TransientFailure):
            service.accept("p1", "app1")
        # The application was durably registered before the "crash".
        state = service.get_application("app1")
        self.assertEqual(state["status"], "waiting")
        self.assertNotIn("result", state)

        fault["raise"] = False
        restarted = self.reopen()
        self.assertEqual(restarted.get_application("app1"), state)
        resumed = restarted.resume_pending()
        self.assertEqual(resumed, ["app1"])
        done = restarted.get_application("app1")
        self.assertEqual(done["status"], "completed")
        self.assertEqual(
            [r["measurement"]["identifier"] for r in done["result"]["records"]], ["m1"]
        )

    def test_auto_resume_on_construction(self):
        fired = {"done": False}

        def hook(stage, context):
            if stage == "build_measurements" and not fired["done"]:
                fired["done"] = True
                raise TransientFailure("boom at build")

        service = self.reopen(hook=hook)
        self.assertEqual(service.accept("p1", "app1")["status"], "waiting")
        restarted = self.reopen(auto_resume=True)
        self.assertEqual(restarted.get_application("app1")["status"], "completed")

    def test_deterministic_build_failure_is_terminal_and_never_rebuilt(self):
        def hook(stage, context):
            if stage == "build_associations":
                raise BuildFailure("corrupt_snapshot", "cannot build")

        service = self.reopen(hook=hook)
        failed = service.accept("p1", "app1")
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["reason"], {"code": "corrupt_snapshot", "message": "cannot build"})
        self.assertNotIn("result", failed)

        rebuilt_calls = {"n": 0}
        def no_fault(stage, context):
            rebuilt_calls["n"] += 1

        service = self.reopen(hook=no_fault)
        self.assertEqual(service.resume_pending(), [])
        state = service.get_application("app1")
        self.assertEqual(state, failed)
        self.assertEqual(rebuilt_calls["n"], 0)

    def test_unreadable_pinned_data_fails_terminally(self):
        # setUp already added m1/alice and a1/alice.
        # Corrupt the stored payload *after* acceptance-time validation.
        with self.service.store.transaction() as session:
            session.execute("UPDATE measurements SET payload = 'not json'")
        state = self.service.accept("p1", "appCorrupt")
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["reason"]["code"], "invalid_stored_data")

    def test_database_contention_is_transient(self):
        import sqlite3 as _sqlite3

        from eligibility_service import service as service_module

        calls = {"n": 0}
        original = service_module.build_result

        def flaky(store, version, purpose, hook=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _sqlite3.OperationalError("database is locked")
            return original(store, version, purpose, hook)

        service_module.build_result = flaky
        try:
            first = self.service.accept("p1", "appBusy")
        finally:
            service_module.build_result = original
        self.assertEqual(first["status"], "waiting")
        retried = self.service.accept("p1", "appBusy")
        self.assertEqual(retried["status"], "completed")


class ConcurrencyTests(ServiceTestCase):
    def test_concurrent_maintenance_is_serial_equivalent(self):
        errors = []

        def worker(index):
            try:
                for step in range(5):
                    self.service.add_measurements(
                        [measurement(f"m{index}s{step}", f"s{index}s{step}")]
                    )
            except Exception as exc:  # pragma: no cover - failure detail
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.service.current_version(), 1 + 20)
        from eligibility_service import dataset

        self.assertEqual(len(dataset.measurements_at(self.store, self.service.current_version())), 20)

    def test_concurrent_retries_complete_exactly_once(self):
        self.service.add_measurements([measurement("m1", "alice")])
        self.service.add_authorizations([auth("a1", "alice")])
        outcomes = []
        errors = []

        def worker():
            try:
                outcomes.append(self.service.accept("p1", "app1"))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(outcomes), 8)
        for outcome in outcomes:
            self.assertEqual(outcome["status"], "completed")
            self.assertEqual(outcome["result"], outcomes[0]["result"])


def build_result_all(store):
    from eligibility_service import build_result

    return build_result(store, store.current_version(), "p1")["records"]


if __name__ == "main":
    unittest.main()
