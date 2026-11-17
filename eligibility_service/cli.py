"""Command line front end (JSON in / JSON out).

Every command prints exactly one JSON object on stdout.  Failures print a JSON
object with ``error`` on stderr and exit with a non-zero status:

======  ==========================================================
status  meaning
======  ==========================================================
0       success / application completed (or still ``waiting`` from a resumed call)
2       illegal input (malformed identifier, JSON, interval, ...)
3       conflict (duplicate identifier, unknown object/application)
4       transient failure - application still ``waiting``, retry later
5       the application is terminally ``failed``
1       unexpected internal error
======  ==========================================================

Batch/object payloads are read either from ``--file`` (a JSON document) or
from ``--json`` (inline).  Use ``-`` as file name to read stdin.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from .errors import (
    ApplicationConflict,
    DuplicateIdentifier,
    EligibilityError,
    InvalidInput,
    MeasurementTooLarge,
    StoreBusy,
    UnknownApplication,
    UnknownObject,
)
from .service import (
    BuildFailure,
    EligibilityService,
    TransientFailure,
)
from .store import Store


def _read_json(source: str) -> Any:
    if source == "-":
        text = sys.stdin.read()
    else:
        with open(source, "r", encoding="utf-8") as handle:
            text = handle.read()
    try:
        return json.loads(text)
    except ValueError as exc:
        raise InvalidInput(f"input is not valid JSON: {exc}") from exc


def _payload(args: argparse.Namespace) -> Any:
    if args.file is not None:
        return _read_json(args.file)
    if args.json is not None:
        try:
            return json.loads(args.json)
        except ValueError as exc:
            raise InvalidInput(f"--json is not valid JSON: {exc}") from exc
    raise InvalidInput("one of --file or --json is required")


def _make_hook(args: argparse.Namespace):
    fault_stage = getattr(args, "fault", None)
    fail_build = getattr(args, "fail_build", None)
    crash_stage = getattr(args, "crash", None)
    if fault_stage is None and fail_build is None and crash_stage is None:
        return None

    def hook(stage: str, context: dict[str, Any]) -> None:
        if crash_stage is not None and stage == crash_stage:
            # Hard process death: no exception handling, no cleanup, no flush.
            os._exit(137)
        if fail_build is not None and stage == fail_build:
            raise BuildFailure("injected_build_failure", f"injected failure at {stage}")
        if fault_stage is not None and stage == fault_stage:
            raise TransientFailure(f"injected transient failure at {stage}")

    return hook


def _emit(payload: Any, stream=sys.stdout) -> None:
    json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
    stream.write("\n")
    stream.flush()


def _parse_id_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except ValueError as exc:
        raise InvalidInput(f"--ids is not valid JSON: {exc}") from exc
    if not isinstance(value, list):
        raise InvalidInput("--ids must be a JSON array of identifier strings")
    return value


def _exit_code(exc: BaseException) -> int:
    if isinstance(exc, TransientFailure):
        return 4
    if isinstance(exc, BuildFailure):
        return 5
    if isinstance(exc, InvalidInput):
        return 2
    if isinstance(exc, MeasurementTooLarge):
        return 2
    if isinstance(exc, StoreBusy):
        return 4
    if isinstance(exc, (DuplicateIdentifier, UnknownObject, UnknownApplication, ApplicationConflict)):
        return 3
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eligibility_service",
        description="Versioned measured-record eligibility service (JSON CLI).",
    )
    parser.add_argument("--db", required=True, help="path to the SQLite database")
    parser.add_argument("--busy-timeout", type=float, default=5.0)
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="do not automatically resume waiting applications on open (diagnostics)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_payload_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--file", help="JSON file with the payload ('-' for stdin)")
        p.add_argument("--json", help="inline JSON payload")

    def add_fault_options(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--fault",
            choices=[
                "accept_after_persist",
                "build_start",
                "build_associations",
                "build_measurements",
                "build_authorizations",
                "build_finish",
            ],
            help="inject a transient fault at this stage (testing/recovery)",
        )
        p.add_argument(
            "--fail-build",
            choices=["build_start", "build_associations", "build_measurements", "build_authorizations"],
            help="inject a deterministic build failure (tests the terminal failed state)",
        )
        p.add_argument(
            "--crash",
            choices=[
                "accept_after_persist",
                "build_start",
                "build_associations",
                "build_measurements",
                "build_authorizations",
                "build_finish",
            ],
            help="kill the process (os._exit) at this stage (tests crash recovery)",
        )

    p_init = sub.add_parser("init", help="create/upgrade the database (idempotent)")
    p_init.set_defaults(func=_cmd_init)

    p_ver = sub.add_parser("version", help="print the current committed revision")
    p_ver.set_defaults(func=_cmd_version)

    for kind, label in (
        ("measurements", "measurements"),
        ("associations", "associations"),
        ("authorizations", "authorizations"),
    ):
        p_add = sub.add_parser(f"add-{kind}", help=f"atomically add a batch of {label}")
        add_payload_options(p_add)
        add_fault_options(p_add)
        p_add.set_defaults(func=_cmd_add, kind=kind)

        p_del = sub.add_parser(f"delete-{kind}", help=f"atomically delete {label} by identifier")
        p_del.add_argument("--ids", required=True, help='JSON array of identifiers, e.g. \'["m1","m2"]\'')
        p_del.set_defaults(func=_cmd_delete, kind=kind)

    for kind, label in (
        ("measurements", "measurements"),
        ("associations", "associations"),
        ("authorizations", "authorizations"),
    ):
        p_rep = sub.add_parser(
            f"replace-{kind}", help=f"atomically replace whole {label} (batch payload)"
        )
        add_payload_options(p_rep)
        p_rep.set_defaults(func=_cmd_replace, kind=kind)

    p_export = sub.add_parser("export", help="accept (or retry) an export application and return its state")
    p_export.add_argument("--purpose", required=True)
    p_export.add_argument("--application-id", help="client chosen identifier; omit for a server-generated one")
    add_fault_options(p_export)
    p_export.set_defaults(func=_cmd_export)

    p_query = sub.add_parser("query", help="query an application's state")
    p_query.add_argument("--application-id", required=True)
    p_query.add_argument("--purpose", help="optional; must match the accepted purpose")
    p_query.set_defaults(func=_cmd_query)

    p_resume = sub.add_parser("resume", help="resume one waiting application")
    p_resume.add_argument("--application-id", required=True)
    add_fault_options(p_resume)
    p_resume.set_defaults(func=_cmd_resume)

    p_resume_all = sub.add_parser("resume-all", help="resume every waiting application")
    add_fault_options(p_resume_all)
    p_resume_all.set_defaults(func=_cmd_resume_all)

    p_snap = sub.add_parser("snapshot", help="show the data visible at a revision (diagnostics)")
    p_snap.add_argument("--version", type=int, required=True)
    p_snap.add_argument("--purpose", required=True)
    add_fault_options(p_snap)
    p_snap.set_defaults(func=_cmd_snapshot)

    p_dump = sub.add_parser("dump", help="dump the raw objects visible at a revision (diagnostics)")
    p_dump.add_argument("--version", type=int, required=True)
    p_dump.set_defaults(no_resume=True)
    p_dump.set_defaults(func=_cmd_dump)

    return parser


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def _open_service(args: argparse.Namespace) -> EligibilityService:
    store = Store(args.db, busy_timeout=args.busy_timeout)
    return EligibilityService(
        store,
        fault_hook=_make_hook(args),
        auto_resume=not getattr(args, "no_resume", False),
    )


def _cmd_init(args: argparse.Namespace) -> dict[str, Any]:
    store = Store(args.db, busy_timeout=args.busy_timeout)
    try:
        return {"ok": True, "version": store.current_version(), "db": args.db}
    finally:
        store.close()


def _cmd_version(args: argparse.Namespace) -> dict[str, Any]:
    store = Store(args.db, busy_timeout=args.busy_timeout)
    try:
        return {"version": store.current_version()}
    finally:
        store.close()


def _cmd_add(args: argparse.Namespace) -> dict[str, Any]:
    payload = _payload(args)
    if not isinstance(payload, list):
        raise InvalidInput("batch payload must be a JSON array")
    service = _open_service(args)
    try:
        method = {
            "measurements": service.add_measurements,
            "associations": service.add_associations,
            "authorizations": service.add_authorizations,
        }[args.kind]
        version = method(payload)
        return {"ok": True, "version": version, "count": len(payload)}
    finally:
        service.store.close()


def _cmd_replace(args: argparse.Namespace) -> dict[str, Any]:
    payload = _payload(args)
    if not isinstance(payload, list):
        raise InvalidInput("replacement payload must be a JSON array")
    service = _open_service(args)
    try:
        method = {
            "measurements": service.replace_measurements,
            "associations": service.replace_associations,
            "authorizations": service.replace_authorizations,
        }[args.kind]
        version = method(payload)
        return {"ok": True, "version": version, "count": len(payload)}
    finally:
        service.store.close()


def _cmd_delete(args: argparse.Namespace) -> dict[str, Any]:
    identifiers = _parse_id_list(args.ids)
    service = _open_service(args)
    try:
        method = {
            "measurements": service.delete_measurements,
            "associations": service.delete_associations,
            "authorizations": service.delete_authorizations,
        }[args.kind]
        version = method(identifiers)
        return {"ok": True, "version": version, "count": len(identifiers)}
    finally:
        service.store.close()


def _cmd_export(args: argparse.Namespace) -> dict[str, Any]:
    service = _open_service(args)
    try:
        return service.accept(args.purpose, args.application_id)
    finally:
        service.store.close()


def _cmd_query(args: argparse.Namespace) -> dict[str, Any]:
    service = _open_service(args)
    try:
        return service.get_application(args.application_id, args.purpose)
    finally:
        service.store.close()


def _cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    service = _open_service(args)
    try:
        return service.resume(args.application_id)
    finally:
        service.store.close()


def _cmd_resume_all(args: argparse.Namespace) -> dict[str, Any]:
    service = _open_service(args)
    try:
        # ``_open_service`` already resumes on open; merge both passes so the
        # report covers every application completed by this invocation.
        recovered = set(service.auto_resumed)
        recovered.update(service.resume_pending())
        return {"ok": True, "resumed": sorted(recovered)}
    finally:
        service.store.close()


def _cmd_dump(args: argparse.Namespace) -> dict[str, Any]:
    from . import dataset

    store = Store(args.db, busy_timeout=args.busy_timeout)
    try:
        version = args.version
        if version < 1:
            raise InvalidInput("version must be a positive integer")
        if not dataset.version_exists(store, version):
            raise UnknownObject("version", str(version))
        return {
            "version": version,
            "measurements": dataset.measurements_at(store, version),
            "associations": [
                {"identifier": ident, "subject_a": sa, "subject_b": sb}
                for ident, sa, sb in dataset.associations_at(store, version)
            ],
            "authorizations": [
                row for row in dataset.all_authorizations_at(store, version)
            ],
        }
    finally:
        store.close()


def _cmd_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    from . import dataset
    from .service import build_result

    service = _open_service(args)
    try:
        if args.version < 1:
            raise InvalidInput("version must be a positive integer")
        if not dataset.version_exists(service.store, args.version):
            raise UnknownObject("version", str(args.version))
        hook = _make_hook(args)
        return build_result(service.store, args.version, args.purpose, hook)
    finally:
        service.store.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = args.func(args)
    except BuildFailure as exc:
        _emit({"error": {"code": exc.reason, "message": exc.message}}, sys.stderr)
        return 5
    except TransientFailure as exc:
        _emit(
            {
                "error": {
                    "code": "transient_failure",
                    "message": str(exc),
                    "note": "application remains waiting; retry with export/query/resume",
                }
            },
            sys.stderr,
        )
        return 4
    except EligibilityError as exc:
        _emit({"error": exc.to_dict()}, sys.stderr)
        return _exit_code(exc)
    _emit(payload)
    if isinstance(payload, dict) and payload.get("status") == "failed":
        return 5
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
