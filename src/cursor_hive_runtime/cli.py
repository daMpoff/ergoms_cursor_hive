import argparse
import json
import sys
import time

from . import __version__
from .controller import control, recover, start_run, status
from .doctor import doctor
from .errors import HiveError


def parser():
    p = argparse.ArgumentParser(prog="cursor-hive")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", help="Private state directory outside source project")
    sub = p.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor", help="Local inventory; no model calls")
    d.add_argument("--cursor")
    start = sub.add_parser("start")
    start.add_argument("project")
    start.add_argument("request")
    start.add_argument("--cursor")
    start.add_argument("--model")
    start.add_argument("--force", action="store_true", help="Explicit Cursor automation profile")
    start.add_argument("--experimental-cursor", action="store_true")
    start.add_argument("--timeout", type=int, default=600)
    start.add_argument("--max-turns", type=int, default=8)
    start.add_argument("--operation-id")
    start.add_argument("--check-argv", help='JSON argv, e.g. ["python", "-m", "unittest"]')
    demo = sub.add_parser("demo", help="Fake transport demonstration; no LLM")
    demo.add_argument("project")
    demo.add_argument("--wait", action="store_true")
    demo.add_argument("--check-argv")
    demo.add_argument("--timeout", type=int, default=30)
    for name in ("status", "wait", "stop"):
        s = sub.add_parser(name)
        s.add_argument("run_id")
        if name == "wait":
            s.add_argument("--timeout", type=int, default=60)
    update = sub.add_parser("update", help="Replace the complete request with a new revision")
    update.add_argument("run_id")
    update.add_argument("request")
    update.add_argument("--expected-revision", required=True, type=int)
    recovery = sub.add_parser("recover")
    recovery.add_argument("run_id")
    recovery.add_argument("destination")
    recovery.add_argument("--worker", choices=["a", "b"])
    sub.add_parser("mcp", help="Stdio MCP, bound by environment for workers")
    daemon = sub.add_parser("_supervise")
    daemon.add_argument("run_directory")
    return p


def wait(home, run_id, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = status(home, run_id)
        if value["state"] in ("exported", "failed", "cancelled", "recovery_required"):
            return value
        if not value["supervisor_alive"]:
            value = status(home, run_id)
            if value["state"] in ("exported", "failed", "cancelled", "recovery_required"):
                return value
            raise HiveError("supervisor_lost", "Run needs recovery; supervisor no longer alive")
        time.sleep(0.1)
    raise HiveError("wait_timeout", "Wait timed out; runtime continues independently")


def main():
    args = parser().parse_args()
    home = args.state_dir
    try:
        if args.command == "mcp":
            from .mcp_server import serve

            serve(home)
            return
        if args.command == "_supervise":
            from pathlib import Path

            from .supervisor import Supervisor

            Supervisor(Path(args.run_directory)).run_loop()
            return
        if args.command == "doctor":
            value = doctor(args.cursor)
        elif args.command in ("start", "demo"):
            checks = json.loads(args.check_argv) if args.check_argv else None
            if args.command == "demo":
                value = start_run(
                    args.project,
                    "Deterministic fake-agent transport demo",
                    home=home,
                    backend="fake",
                    timeout=args.timeout,
                    check_argv=checks,
                )
                if args.wait:
                    value = wait(home, value["run_id"], args.timeout + 5)
            else:
                value = start_run(
                    args.project,
                    args.request,
                    home=home,
                    cursor=args.cursor,
                    model=args.model,
                    force=args.force,
                    timeout=args.timeout,
                    max_turns=args.max_turns,
                    operation_id=args.operation_id,
                    check_argv=checks,
                    experimental_cursor=args.experimental_cursor,
                )
        elif args.command == "status":
            value = status(home, args.run_id)
        elif args.command == "wait":
            value = wait(home, args.run_id, args.timeout)
        elif args.command == "update":
            value = control(
                home,
                args.run_id,
                "update",
                {"request": args.request, "expected_revision": args.expected_revision},
            )
        elif args.command == "stop":
            value = control(home, args.run_id, "stop")
        else:
            value = recover(home, args.run_id, args.destination, args.worker)
        print(json.dumps(value, ensure_ascii=False, indent=2))
        if value.get("state") in ("failed", "recovery_required"):
            sys.exit(1)
    except (HiveError, ValueError) as e:
        error = (
            e.as_dict()
            if isinstance(e, HiveError)
            else {"code": "invalid_input", "message": str(e)}
        )
        print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
        sys.exit(2)
