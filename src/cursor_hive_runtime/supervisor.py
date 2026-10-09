import os
import secrets
import sys
import time
from pathlib import Path

from .adapters import approve_worker_mcp, configure_worker, cursor_argv, parse_stream, peek_session
from .board import Board
from .errors import HiveError
from .processes import OwnedProcess, ProjectLock, identity
from .rpc import RuntimeAPI, start_server
from .util import atomic_json, read_json
from .workspace import capture, export_delta, materialize, prepare, snapshot


def ready_tasks(board):
    tasks = board.context()["tasks"]
    states = {t["id"]: t["state"] for t in tasks}
    return any(
        t["state"] == "available" and all(states.get(d) == "completed" for d in t["dependencies"])
        for t in tasks
    )


def should_wake(board, actor):
    marker = board.meta(f"yield:{actor}")
    if not marker:
        return False
    if ready_tasks(board):
        return True
    with board.connection() as db:
        events = db.execute(
            "SELECT kind,payload FROM events WHERE sequence>?", (marker["sequence"],)
        ).fetchall()
    import json

    return any(
        row["kind"] in ("publish", "task_complete", "task_create", "update")
        and json.loads(row["payload"]).get("actor") != actor
        for row in events
    )


def verify_candidate(run, manifest, argv, deadline, cancelled=lambda: False):
    if not argv:
        return {"observed": False, "reason": "No check profile configured", "passed": None}
    check = run / "verification"
    materialize(run, manifest, check)
    # Private Git administration lets tools query paths without reaching an ancestor.
    from .workspace import git

    git(check, "init", "--quiet")
    git(check, "add", "--all")
    with (run / "check.stdout.log").open("wb") as out, (run / "check.stderr.log").open("wb") as err:
        env = {k: v for k, v in os.environ.items() if not k.startswith("CURSOR_HIVE_")}
        child = OwnedProcess(argv, cwd=check, env=env, stdout=out, stderr=err)
        child.save(run / "process-check.json")
        end = time.monotonic() + deadline
        timed_out = False
        was_cancelled = False
        try:
            while child.child.poll() is None:
                child.observe()
                if cancelled():
                    was_cancelled = True
                    break
                if time.monotonic() >= end:
                    timed_out = True
                    break
                time.sleep(0.05)
        finally:
            child.save(run / "process-check.json")
            child.stop()
            child.save(run / "process-check.json")
        result = child.child.returncode
    after = snapshot(check, run, list(manifest))["files"]
    unchanged = after == manifest
    return {
        "observed": True,
        "argv": argv,
        "exit_code": result,
        "timed_out": timed_out,
        "cancelled": was_cancelled,
        "inputs_unchanged": unchanged,
        "inputs": manifest,
        "passed": result == 0 and not timed_out and not was_cancelled and unchanged,
        "environment": {"python": sys.version.split()[0], "os": os.name},
    }


class Supervisor:
    def __init__(self, run):
        self.run = run
        self.config = read_json(run / "config.json")
        self.board = Board(run / "state.sqlite")
        self.api = RuntimeAPI(self.board, run, self.config["controller_token"])
        self.processes, self.logs, self.sessions, self.turns = {}, {}, {}, {"a": 0, "b": 0}
        self.server = None

    def launch(self, actor):
        self.turns[actor] += 1
        if self.turns[actor] > self.config["max_turns"]:
            raise HiveError("turn_limit", f"Worker {actor} reached turn limit")
        token = secrets.token_urlsafe(32)
        self.api.bind(actor, token)
        self.board.set_meta(f"yield:{actor}", None)
        if self.board.meta(f"ack:{actor}") != self.board.meta("revision"):
            self.board.set_meta(f"ack:{actor}", None)
        env = configure_worker(self.run, actor, self.url, token)
        workspace = self.run / "workers" / actor
        if self.config["backend"] == "fake":
            argv = [sys.executable, "-m", "cursor_hive_runtime.fake_agent"]
        else:
            if self.turns[actor] == 1:
                approve_worker_mcp(self.config["cursor"], workspace)
            policy = (Path(__file__).with_name("worker-policy.md")).read_text(encoding="utf-8")
            prompt = (
                f"CursorHive worker {actor}; request revision {self.board.meta('revision')}.\n"
                f"{policy}\n\nREQUEST:\n{self.board.meta('request')}\n"
                "Read the board and acknowledge this revision before mutations."
            )
            argv = cursor_argv(
                self.config["cursor"],
                workspace,
                prompt,
                self.sessions.get(actor),
                self.config.get("model"),
                self.config["force"],
            )
        log = self.run / f"worker-{actor}-{self.turns[actor]}.ndjson"
        errlog = self.run / f"worker-{actor}-{self.turns[actor]}.stderr.log"
        out, err = log.open("wb"), errlog.open("wb")
        self.logs[actor] = (out, err, log, errlog)
        self.processes[actor] = OwnedProcess(argv, cwd=workspace, env=env, stdout=out, stderr=err)
        self.processes[actor].save(self.run / f"process-{actor}.json")
        self.board.set_meta(
            f"worker:{actor}",
            {"turn": self.turns[actor], "state": "active", "revision": self.board.meta("revision")},
        )

    def stop_all(self):
        errors = []
        for actor, child in list(self.processes.items()):
            self.api.bind(actor, None)
            if (
                self.config["backend"] == "cursor"
                and actor not in self.sessions
                and actor in self.logs
            ):
                session = peek_session(self.logs[actor][2])
                if session:
                    self.sessions[actor] = session
            try:
                child.save(self.run / f"process-{actor}.json")
                child.stop()
                child.save(self.run / f"process-{actor}.json")
                self.board.set_meta(
                    f"worker:{actor}", {"state": "stopped", "turn": self.turns[actor]}
                )
                del self.processes[actor]
            except Exception as e:
                errors.append(str(e))
            for stream in self.logs.get(actor, ())[:2]:
                stream.close()
        if errors:
            raise HiveError("shutdown_unconfirmed", "; ".join(errors))

    def run_loop(self):
        safe_shutdown = False
        lock = ProjectLock(Path(self.config["home"]), Path(self.config["project"]))
        started = time.monotonic()
        try:
            self.server, self.url = start_server(self.api)
            atomic_json(
                self.run / "endpoint.json",
                {
                    "url": self.url,
                    "token": self.config["controller_token"],
                    "process": identity(os.getpid()),
                },
            )
            baseline = capture(Path(self.config["project"]), self.run)
            prepare(self.run, baseline)
            self.board.set_meta("state", "running")
            self.launch("a")
            current_revision = self.board.meta("revision")
            candidate = None
            while True:
                if self.board.meta("stop_requested"):
                    self.board.set_meta("state", "stopping")
                    break
                if time.monotonic() - started > self.config["timeout"]:
                    raise HiveError("deadline", "Run deadline exceeded")
                revision = self.board.meta("revision")
                if revision != current_revision:
                    self.stop_all()
                    self.sessions = {k: v for k, v in self.sessions.items() if v}
                    current_revision = revision
                    self.launch("a")
                candidate = self.board.close_admission()
                if candidate:
                    break
                if "b" not in self.processes and ready_tasks(self.board):
                    self.launch("b")
                for actor, child in list(self.processes.items()):
                    child.observe()
                    out, err, log, errlog = self.logs[actor]
                    if any(p.stat().st_size > 10 * 1024 * 1024 for p in (log, errlog)):
                        raise HiveError("log_limit", "Worker output exceeded 10 MiB")
                    code = child.child.poll()
                    if code is None:
                        continue
                    out.flush()
                    if code != 0:
                        raise HiveError(
                            "worker_failed", f"Worker {actor} exit {code}; private logs retained"
                        )
                    if self.config["backend"] == "cursor" and actor not in self.sessions:
                        self.sessions[actor] = parse_stream(log)["session_id"]
                    elif self.config["backend"] == "cursor":
                        if parse_stream(log)["session_id"] != self.sessions[actor]:
                            raise HiveError("session_changed", "Resume returned another session")
                    if self.config["backend"] == "cursor" and should_wake(self.board, actor):
                        child.stop()
                        out.close()
                        err.close()
                        self.launch(actor)
                if "b" not in self.processes and time.monotonic() - started > min(
                    120, self.config["timeout"]
                ):
                    raise HiveError(
                        "bootstrap_stalled", "First worker did not expose parallel work"
                    )
                self.api.changed.wait(0.1)
                self.api.changed.clear()
            self.stop_all()
            safe_shutdown = True
            if candidate:
                receipt = verify_candidate(
                    self.run,
                    candidate["manifest"],
                    self.config.get("check_argv"),
                    self.config["check_timeout"],
                    lambda: self.board.meta("stop_requested"),
                )
                atomic_json(self.run / "check-receipt.json", receipt)
                if self.board.meta("stop_requested"):
                    self.board.set_meta("state", "cancelled")
                    return
                contract = export_delta(self.run, candidate["manifest"], self.run / "export")
                report = {
                    "run_id": self.config["run_id"],
                    "state": "exported",
                    "backend": self.config["backend"],
                    "revision": candidate["revision"],
                    "summary": candidate["summary"],
                    "export": str(self.run / "export"),
                    "changed_files": list(contract["changed"]),
                    "deleted_files": contract["deleted"],
                    "checks": {k: v for k, v in receipt.items() if k != "inputs"},
                    "ready": False,
                    "verification_required": True,
                    "usage": None,
                    "turns": self.turns,
                    "workspaces_retained": True,
                }
                atomic_json(self.run / "report.json", report)
                self.board.set_meta("report", report)
                self.board.set_meta("state", "exported")
            else:
                self.board.set_meta("state", "cancelled")
        except Exception as e:
            try:
                self.stop_all()
                safe_shutdown = True
            except Exception:
                safe_shutdown = False
            error = (
                e.as_dict()
                if isinstance(e, HiveError)
                else {"code": "runtime_failed", "message": str(e)[:1000]}
            )
            self.board.set_meta("error", error)
            self.board.set_meta("state", "failed" if safe_shutdown else "recovery_required")
        finally:
            from .processes import alive

            known_writers = []
            for path in self.run.glob("process-*.json"):
                record = read_json(path)
                known_writers.extend(
                    r for r in [record["parent"], *record["descendants"]] if r and alive(r)
                )
            if known_writers:
                safe_shutdown = False
                self.board.set_meta("state", "recovery_required")
            if safe_shutdown:
                lock.release(self.config["run_id"])
            if self.server:
                self.server.shutdown()
                self.server.server_close()
