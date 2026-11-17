#!/usr/bin/env python3
"""Independent black-box acceptance suite.

This harness deliberately imports *nothing* from ``eligibility_service``.  It
drives the command line interface in a subprocess, exactly like an external
client, and checks only the documented JSON contract.  The three mandatory
verification areas are:

* eligibility adjudication (allow/deny, purpose, half-open intervals,
  connected components, full member sets, ASCII ordering, duplicates);
* version isolation (a later revision never changes an old application, a
  retry returns the frozen result, restarts do not mix versions);
* interruption recovery (waiting -> completed after a restart, terminal
  failure is never rebuilt).

Run with::

    python3 acceptance/blackbox_acceptance.py

Exit code 0 means every check passed.  ``--keep`` leaves the temporary
database behind for inspection.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_NAME = "acceptance.sqlite"

PASSED = 0
FAILED: list[str] = []


def check(condition: bool, label: str, detail: Any = None) -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label}")
        if detail is not None:
            print(f"       detail: {json.dumps(detail, sort_keys=True)[:2000]}")


def eq(actual: Any, expected: Any, label: str) -> None:
    check(actual == expected, label, {"expected": expected, "actual": actual})


class Client:
    """Thin wrapper around ``python -m eligibility_service``."""

    def __init__(self, db: str) -> None:
        self.db = db
        # -I keeps the interpreter isolated (no cwd/PYTHONPATH hijack); the
        # package location is injected explicitly instead.
        bootstrap = (
            "import sys; sys.path.insert(0, %r); "
            "from eligibility_service.cli import main; sys.exit(main())" % ROOT
        )
        self.base = [sys.executable, "-I", "-c", bootstrap, "--db", db]
        self.env = dict(os.environ)

    def run(self, *args: str, stdin: str | None = None) -> tuple[int, Any, str]:
        proc = subprocess.run(
            self.base + list(args),
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=tempfile.gettempdir(),
        )
        out: Any = None
        for stream in (proc.stdout, proc.stderr):
            if out is None and stream.strip().startswith("{"):
                try:
                    out = json.loads(stream)
                except ValueError:
                    pass
        return proc.returncode, out, proc.stderr

    # convenience wrappers -------------------------------------------------
    def add(self, kind: str, batch: list[dict]) -> tuple[int, Any]:
        code, out, _ = self.run(f"add-{kind}", "--json", json.dumps(batch))
        return code, out

    def replace(self, kind: str, batch: list[dict]) -> tuple[int, Any]:
        code, out, _ = self.run(f"replace-{kind}", "--json", json.dumps(batch))
        return code, out

    def delete(self, kind: str, ids: list[str]) -> tuple[int, Any]:
        code, out, _ = self.run(f"delete-{kind}", "--ids", json.dumps(ids))
        return code, out

    def export(self, purpose: str, app_id: str, *extra: str, no_resume: bool = False) -> tuple[int, Any]:
        args = ["export", "--purpose", purpose, "--application-id", app_id, *extra]
        if no_resume:
            args.insert(0, "--no-resume")
        code, out, _ = self.run(*args)
        return code, out

    def query(self, app_id: str, purpose: str | None = None, *, no_resume: bool = False) -> tuple[int, Any]:
        args = ["query", "--application-id", app_id]
        if purpose is not None:
            args += ["--purpose", purpose]
        if no_resume:
            args.insert(0, "--no-resume")
        code, out, _ = self.run(*args)
        return code, out


def m(ident: str, subject: str, at: int = 100, payload: Any = None) -> dict:
    return {
        "identifier": ident,
        "subject": subject,
        "collected_at": at,
        "payload": payload if payload is not None else {"id": ident},
    }


def e(ident: str, a: str, b: str) -> dict:
    return {"identifier": ident, "subject_a": a, "subject_b": b}


def a(ident: str, subject: str, decision: str = "allow", start: int = 0, end: int = 1000, purpose: str = "p1") -> dict:
    return {
        "identifier": ident,
        "subject": subject,
        "purpose": purpose,
        "decision": decision,
        "start_at": start,
        "end_at": end,
    }


def record_ids(response: dict) -> list[str]:
    return [r["measurement"]["identifier"] for r in response["result"]["records"]]


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


def scenario_eligibility(client: Client) -> None:
    print("\n[1] eligibility adjudication")
    code, out = client.add("measurements", [
        m("m1", "alice", 100),
        m("m2", "bob", 100),
        m("m3", "carol", 100),
        m("m4", "dave", 100),
        m("m5", "erin", 100),
        m("m6", "frank", 100),
    ])
    eq(code, 0, "add measurements accepted")
    eq(out["version"], 2, "first revision is 2")

    client.add("associations", [
        e("z9", "alice", "bob"),
        e("z8", "bob", "alice"),      # parallel edge
        e("z7", "bob", "bob"),        # self loop
        e("z6", "carol", "dave"),     # separate component
        e("z5", "erin", "frank"),
    ])
    client.add("authorizations", [
        a("authbob", "bob"),                                  # alice's component
        a("authbobdeny", "bob", decision="deny", start=500, end=600),  # not at t=100
        a("authdave", "dave"),                                # carol's component
        a("authdavedeny", "dave", decision="deny"),          # denial -> carol excluded
        a("authotherpurpose", "frank", purpose="p2"),        # wrong purpose -> erin excluded
        a("authunrelated", "nobody"),                         # never a member
    ])

    code, app = client.export("p1", "appMain")
    eq(code, 0, "export accepted")
    eq(app["status"], "completed", "export completed")
    eq(app["accepted_version"], 4, "accepted version pinned to accepted revision")
    eq(record_ids(app), ["m1", "m2"], "only the component with an allow is eligible")
    record = app["result"]["records"][0]
    eq(record["measurement"], m("m1", "alice", 100), "full original data returned")
    eq(record["members"], ["alice", "bob"], "complete member set returned")
    eq(record["authorizations"], ["authbob"], "only applicable authorizations listed")

    # half-open interval semantics at the exact boundary
    client.delete("authorizations", ["authbob"])
    client.add("measurements", [m("m7", "gina", 200)])
    client.add("authorizations", [
        a("authginaleft", "gina", start=200, end=201),
        a("authginaright", "gina", start=199, end=200),
    ])
    code, app = client.export("p1", "appInterval")
    eq(record_ids(app), ["m7"], "left-closed boundary is included")
    eq(app["result"]["records"][0]["authorizations"], ["authginaleft"],
       "right-open boundary excludes the ending authorization")

    # purpose isolation
    code, app2 = client.export("p2", "appP2")
    eq(code, 0, "second purpose accepted")
    eq(record_ids(app2), ["m5", "m6"], "purposes are isolated (p1 authorizations do not leak)")

    # legal empty result completes
    code, empty = client.export("pNoAuthHere", "appEmpty")
    eq(code, 0, "empty export accepted")
    eq(empty["status"], "completed", "legal empty result is completed")
    eq(empty["result"]["records"], [], "empty record list")

    # identical payloads are not merged
    client.add("measurements", [m("m8", "gina", 200, {"same": 1}), m("m9", "gina", 200, {"same": 1})])
    code, app = client.export("p1", "appTwins")
    twins = [r["measurement"]["identifier"] for r in app["result"]["records"]]
    eq(twins.count("m8"), 1, "first identical record kept separately")
    eq(twins.count("m9"), 1, "second identical record kept separately")
    eq(twins, sorted(twins), "records sorted by ASCII identifier")

    # ASCII ordering, including digits before letters
    client.add("measurements", [m("zLast", "gina", 200), m("AFirst", "gina", 200)])
    code, app = client.export("p1", "appOrder")
    ids = record_ids(app)
    eq(ids, sorted(ids), "ASCII ordering with mixed case")
    check(ids[0] == "AFirst", "uppercase sorts before lowercase", ids[:3])


def scenario_maintenance(client: Client) -> None:
    print("\n[2] maintenance legality and atomicity")
    before = current_version(client)

    code, out = client.add("measurements", [m("dup", "s"), m("dup", "s")])
    eq(code, 3, "duplicate identifier inside batch rejected")
    eq(out["error"]["code"], "duplicate_identifier", "duplicate error code")
    eq(current_version(client), before, "rejected duplicate batch does not advance version")

    code, out = client.add("measurements", [m("goodOne", "s"), m("bad id", "s")])
    eq(code, 2, "illegal identifier rejected")
    eq(current_version(client), before, "illegal identifier does not advance version")
    code, out = client.add("measurements", [m("goodOne", "s")])
    eq(code, 0, "good identifier from rejected batch was not created earlier")

    code, out = client.add("associations", [e("m1", "x", "y")])
    eq(code, 0, "same spelling may be used by a different kind")
    code, out = client.add("associations", [e("m1", "z", "w")])
    eq(code, 3, "duplicate inside its own kind rejected")
    eq(out["error"]["code"], "duplicate_identifier", "duplicate error code")
    code, out = client.delete("associations", ["m1"])
    eq(code, 0, "association with the shared spelling deletable")
    code, out = client.delete("measurements", ["nopeMeasurement"])
    eq(code, 3, "deleting an unknown measurement rejected")
    eq(out["error"]["code"], "unknown_object", "unknown delete target code")

    code, out = client.add("authorizations", [a("badInterval", "s", start=5, end=5)])
    eq(code, 2, "empty authorization interval rejected")
    code, out = client.add("authorizations", [a("badInterval", "s", start=6, end=5)])
    eq(code, 2, "inverted authorization interval rejected")
    code, _, _ = client.run("add-measurements", "--json", json.dumps([m("sub1", "s")]))
    eq(code, 0, "measurement added before subcommand check")
    code, out, _ = client.run("not-a-command")
    eq(code, 2, "unknown command rejected")

    code, out = client.replace("measurements", [m("doesNotExist", "s")])
    eq(code, 3, "replace of unknown object rejected")
    eq(out["error"]["code"], "unknown_object", "unknown object code")

    client.add("authorizations", [a("authMaintenance", "alice", purpose="pMaint")])
    before_delete = current_version(client)
    code, out = client.delete("measurements", ["m1", "missingId"])
    eq(code, 3, "delete batch containing unknown id rejected")
    eq(current_version(client), before_delete, "aborted delete batch did not advance version")
    code, snap, _ = client.run(
        "snapshot", "--version", str(before_delete), "--purpose", "pMaint"
    )
    eq(code, 0, "snapshot of pre-delete version readable")
    check(any(r["measurement"]["identifier"] == "m1" for r in snap["records"]),
          "aborted delete batch changed nothing")

    code, out = client.add("measurements", [{"identifier": "nanPayload", "subject": "s", "collected_at": 1, "payload": float("nan")}])
    eq(code, 2, "NaN payload rejected")
    code, out = client.add("measurements", [{"identifier": "floatTime", "subject": "s", "collected_at": 1.5, "payload": 1}])
    eq(code, 2, "non-integer collection time rejected")
    code, out, _ = client.run("add-measurements", "--json", '{"not": "an array"}')
    eq(code, 2, "non-array batch rejected")


def scenario_version_isolation(client: Client) -> None:
    print("\n[3] version isolation")
    client.add("measurements", [m("iso1", "xavier", 100)])
    client.add("associations", [e("isoEdge", "xavier", "yvonne")])
    client.add("authorizations", [a("isoAuth", "yvonne", purpose="pIsolation")])

    code, first = client.export("pIsolation", "appFrozen")
    eq(code, 0, "isolation export accepted")
    eq(record_ids(first), ["iso1"], "eligible before revision")
    frozen_version = first["accepted_version"]

    # Mutate everything that could change the answer.
    client.replace("authorizations", [a("isoAuth", "yvonne", decision="deny", purpose="pIsolation")])
    client.replace("associations", [e("isoEdge", "xavier", "zachary")])
    client.replace("measurements", [m("iso1", "xavier", 999, {"changed": True})])
    client.add("measurements", [m("iso2", "xavier", 100)])
    client.delete("associations", ["isoEdge"])
    code, later = client.export("pIsolation", "appLater")
    check(later["accepted_version"] > frozen_version, "new application sees a newer version")
    eq(record_ids(later), [], "later revisions changed the later result only")

    # Retry / query of the frozen application still returns the old answer.
    code, retried = client.export("pIsolation", "appFrozen")
    eq(code, 0, "retry accepted")
    eq(retried, first, "retry returns the exact frozen result")
    code, queried = client.query("appFrozen")
    eq(queried, first, "query returns the exact frozen result")
    eq(first["result"]["version"], frozen_version, "result payload documents its version")
    eq(first["result"]["records"][0]["measurement"]["collected_at"], 100,
       "old measurement content survives replacement")

    # A restart must not reinterpret the old application.
    code, queried_after_restart = client.query("appFrozen")
    eq(queried_after_restart, first, "restart does not mix versions")

    # Purpose conflicts and unknown applications.
    code, out = client.export("pOther", "appFrozen")
    eq(code, 3, "retry with different purpose rejected")
    eq(out["error"]["code"], "application_conflict", "purpose conflict code")
    code, out = client.query("appFrozen", "pOther")
    eq(code, 3, "query with different purpose rejected")
    code, out = client.query("neverIssued")
    eq(code, 3, "query of unknown application rejected")
    eq(out["error"]["code"], "unknown_application", "unknown application code")


def scenario_recovery(client: Client) -> None:
    print("\n[4] interruption recovery")
    # Transient failure: stays waiting, then completes on retry.
    code, out = client.export("p1", "appTransient", "--fault", "build_measurements")
    eq(code, 0, "accept with transient fault returns normally")
    eq(out["status"], "waiting", "transiently failed application is waiting")
    check("result" not in out, "waiting application has no result")
    # Any ordinary CLI invocation resumes pending work, so observe the
    # waiting state with --no-resume (a read-only inspection mode).
    code, queried = client.query("appTransient", no_resume=True)
    eq(queried["status"], "waiting", "query reports waiting")

    # A crash *after* durable registration: export exits non-zero, but the
    # application must exist and be resumable.
    # --no-resume keeps the earlier waiting application waiting as well, so the
    # following resume-all has to pick up two applications.
    code, out, err = client.run(
        "--no-resume", "export", "--purpose", "p1", "--application-id", "appCrash",
        "--fault", "accept_after_persist",
    )
    eq(code, 4, "crash-after-persist reports transient exit code")
    code, state = client.query("appCrash", no_resume=True)
    eq(code, 0, "durably registered before the crash")
    eq(state["status"], "waiting", "crashed application is waiting after restart")

    # resume-all picks both up (the "restart" is a new process with the same DB).
    code, out, _ = client.run("resume-all")
    eq(code, 0, "resume-all succeeded")
    eq(sorted(out["resumed"]), ["appCrash", "appTransient"], "both waiting applications resumed")
    for app_id in ("appCrash", "appTransient"):
        code, state = client.query(app_id)
        eq(state["status"], "completed", f"{app_id} completed after resume")
        eq(state["accepted_version"], state["result"]["version"],
           f"{app_id} result built from its accepted version")

    # Hard crash (os._exit, no cleanup) mid-build after durable registration.
    code, out, err = client.run(
        "export", "--purpose", "p1", "--application-id", "appKilled",
        "--crash", "build_measurements",
    )
    eq(code, 137, "process killed during the build")
    eq(out, None, "killed process returned no response")
    code, state = client.query("appKilled", no_resume=True)
    eq(code, 0, "killed application still registered")
    eq(state["status"], "waiting", "killed application left waiting, not failed")
    check("result" not in state and "reason" not in state, "no partial result persisted")
    code, out, _ = client.run("resume", "--application-id", "appKilled")
    eq(code, 0, "killed application resumes")
    eq(out["status"], "completed", "killed application completes after restart")

    # Kill at the very last stage: the result must not be half-written.
    code, out, err = client.run(
        "export", "--purpose", "p1", "--application-id", "appKilledLate",
        "--crash", "build_finish",
    )
    eq(code, 137, "process killed at the final build stage")
    code, state = client.query("appKilledLate", no_resume=True)
    eq(state["status"], "waiting", "late kill still leaves waiting")
    code, out, _ = client.run("resume-all")
    check("appKilledLate" in out["resumed"], "late-killed application resumed")

    # Deterministic build failure: terminal, reason present, never rebuilt.
    code, out = client.export("p1", "appBroken", "--fail-build", "build_associations")
    eq(code, 5, "terminal failed application reports exit code 5")
    eq(out["status"], "failed", "application failed")
    check("result" not in out, "failed application has no result")
    code, reason = client.query("appBroken")
    eq(code, 5, "querying a failed application also signals failure")
    check(bool(reason.get("reason", {}).get("code")), "failure reason recorded")
    failed_state = dict(reason)

    code, out, _ = client.run("resume", "--application-id", "appBroken")
    eq(code, 5, "resume of terminal failure stays failed")
    eq(out, failed_state, "terminal state is immutable")

    code, out, _ = client.run("resume-all")
    check("appBroken" not in out["resumed"], "failed application is not resumed/rebuilt")
    code, again = client.query("appBroken", no_resume=True)
    eq(again, failed_state, "failed state unchanged by a restart and resume sweep")

    # Idempotent retries still work after all of that.
    code, out = client.export("p1", "appTransient")
    eq(out["status"], "completed", "completed application remains completed")


def scenario_concurrency(db: str) -> None:
    print("\n[5] concurrent serial equivalence (processes)")

    def add_worker(index: int) -> subprocess.Popen:
        batch = json.dumps([m(f"c{index}x{step}", f"s{index}x{step}") for step in range(5)])
        env = dict(os.environ)
        bootstrap = (
            "import sys; sys.path.insert(0, %r); "
            "from eligibility_service.cli import main; sys.exit(main())" % ROOT
        )
        return subprocess.Popen(
            [sys.executable, "-I", "-c", bootstrap, "--db", db,
             "add-measurements", "--json", batch],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            cwd=tempfile.gettempdir(),
        )

    procs = [add_worker(i) for i in range(6)]
    versions = []
    for proc in procs:
        out, err = proc.communicate(timeout=120)
        check(proc.returncode == 0, "concurrent add succeeded", err[-400:])
        if out.strip():
            versions.append(json.loads(out)["version"])
    eq(len(versions), 6, "six concurrent writers reported versions")
    eq(len(set(versions)), 6, "every writer received a distinct new version")

    client = Client(db)
    expected = [f"c{i}x{step}" for i in range(6) for step in range(5)]
    # Every concurrently added measurement must be retrievable at its version.
    code, out, _ = client.run("dump", "--version", str(max(versions)))
    eq(code, 0, "dump of the last concurrent version readable")
    present = {r["identifier"] for r in out["measurements"]}
    eq(sorted(present & set(expected)), sorted(expected), "no concurrent insert lost")


def current_version(client: Client) -> int:
    code, out, _ = client.run("version")
    return int(out["version"])


def scenario_restart_resume(db: str) -> None:
    print("\n[6] automatic resume on restart")
    client = Client(db)
    code, out = client.export("pRestart", "appRestart", "--fault", "build_authorizations")
    eq(out["status"], "waiting", "application registered waiting")
    code, out, _ = client.run("resume-all")
    eq(out["resumed"], ["appRestart"], "resume-all completes it")
    code, state = client.query("appRestart")
    eq(state["status"], "completed", "completed after restart")
    eq(state["result"]["records"], [], "empty but legal result")


def main(argv: list[str]) -> int:
    keep = "--keep" in argv
    workdir = tempfile.mkdtemp(prefix="eligibility-acceptance-")
    db = os.path.join(workdir, DB_NAME)
    try:
        client = Client(db)
        code, out, err = client.run("init")
        check(code == 0 and out["version"] == 1, "database initialises at version 1", err)
        scenario_eligibility(client)
        scenario_maintenance(client)
        scenario_version_isolation(client)
        scenario_recovery(client)
        scenario_concurrency(db)
        scenario_restart_resume(db)
    finally:
        if keep:
            print(f"\ndatabase kept at {db}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)

    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    if FAILED:
        for label in FAILED:
            print(f"  - {label}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
