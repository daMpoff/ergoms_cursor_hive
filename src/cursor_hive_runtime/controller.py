import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .adapters import permissions
from .board import Board
from .errors import HiveError
from .processes import ProjectLock, alive, identity
from .rpc import Client
from .util import atomic_json, canonical, digest, read_json, state_home


def run_path(home, run_id):
    try:
        if str(uuid.UUID(run_id)) != run_id:
            raise ValueError()
    except (ValueError, TypeError, AttributeError) as e:
        raise HiveError("invalid_run", "Expected a canonical run UUID") from e
    run = state_home(home) / "runs" / run_id
    if not (run / "config.json").is_file():
        raise HiveError("not_found", "Run not found")
    return run


def start_run(
    project,
    request,
    *,
    home=None,
    operation_id=None,
    backend="cursor",
    cursor=None,
    model=None,
    force=False,
    experimental_cursor=False,
    timeout=600,
    max_turns=8,
    check_argv=None,
    check_timeout=60,
):
    if os.environ.get("CURSOR_HIVE_WORKER"):
        raise HiveError("nested_start", "Worker cannot start another run")
    home, project = state_home(home), Path(project).expanduser().resolve()
    if (
        not project.is_dir()
        or not isinstance(request, str)
        or not request.strip()
        or len(request) > 64000
    ):
        raise HiveError("invalid_request", "Select a project and a nonempty request")
    if home.is_relative_to(project) or project.is_relative_to(home):
        raise HiveError("overlapping_state", "Run storage and source project must be disjoint")
    if backend not in ("cursor", "fake"):
        raise HiveError("invalid_backend", "Backend must be cursor or fake")
    if not 1 <= timeout <= 7200 or not 1 <= max_turns <= 32 or not 1 <= check_timeout <= 600:
        raise HiveError("invalid_limits", "Unsupported runtime limits")
    if check_argv is not None and (
        not isinstance(check_argv, list)
        or not check_argv
        or any(not isinstance(x, str) or not x for x in check_argv)
    ):
        raise HiveError("invalid_check", "Check profile must be a nonempty argv list")
    executable = cursor or shutil.which("agent") or shutil.which("cursor-agent")
    if backend == "cursor":
        if not executable:
            raise HiveError("cursor_missing", "Cursor CLI not found; run cursor-hive doctor")
        if not experimental_cursor:
            raise HiveError(
                "qualification_required",
                "Cursor adapter is experimental; live qualification is pending",
            )
    operation_id = operation_id or str(uuid.uuid4())
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 160:
        raise HiveError("invalid_operation", "Invalid start operation_id")
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(home) + ":" + operation_id))
    run = home / "runs" / run_id
    signature = digest(
        canonical(
            [
                str(project),
                request,
                backend,
                executable,
                model,
                force,
                timeout,
                max_turns,
                check_argv,
                check_timeout,
            ]
        ).encode()
    )
    if run.exists():
        cfg = read_json(run / "config.json")
        if cfg["start_signature"] != signature:
            raise HiveError("operation_conflict", "Start operation_id has different arguments")
        return {"run_id": run_id, "state": Board(run / "state.sqlite").meta("state")}
    lock = ProjectLock(home, project)
    lock.acquire(run_id)
    try:
        run.mkdir(parents=True, mode=0o700)
        config = {
            "run_id": run_id,
            "home": str(home),
            "project": str(project),
            "backend": backend,
            "cursor": executable,
            "model": model,
            "force": force,
            "timeout": timeout,
            "max_turns": max_turns,
            "check_argv": check_argv,
            "check_timeout": check_timeout,
            "permissions": permissions(project)
            if backend == "cursor"
            else {"allow": [], "deny": []},
            "controller_token": secrets.token_urlsafe(32),
            "start_signature": signature,
        }
        atomic_json(run / "config.json", config)
        board = Board(run / "state.sqlite")
        board.initialize(run_id, project, request)
        flags = (
            {"start_new_session": True}
            if os.name != "nt"
            else {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            }
        )
        with (
            (run / "supervisor.stdout.log").open("wb") as out,
            (run / "supervisor.stderr.log").open("wb") as err,
        ):
            child = subprocess.Popen(
                [sys.executable, "-m", "cursor_hive_runtime", "_supervise", str(run)],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                **flags,
            )
        atomic_json(run / "supervisor.json", identity(child.pid))
        threading.Thread(target=child.wait, daemon=True).start()
        # Popen does not retain pipe handles; this object is intentionally not a lifecycle owner.
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if (run / "endpoint.json").exists() or child.poll() is not None:
                break
            time.sleep(0.025)
        return {"run_id": run_id, "state": board.meta("state"), "backend": backend}
    except Exception:
        lock.release(run_id)
        raise


def client_for(run):
    endpoint = read_json(run / "endpoint.json")
    return Client(endpoint["url"], endpoint["token"])


def status(home, run_id):
    run = run_path(home, run_id)
    board = Board(run / "state.sqlite")
    value = board.context()
    value["error"] = board.meta("error")
    value["workers"] = {a: board.meta(f"worker:{a}") for a in ("a", "b")}
    value["supervisor_alive"] = False
    if (run / "endpoint.json").exists():
        try:
            client_for(run).call("context", {"limit": 1})
            value["supervisor_alive"] = True
        except HiveError:
            pass
    return value


def control(home, run_id, method, args=None, operation_id=None):
    return client_for(run_path(home, run_id)).call(
        method, args or {}, operation_id or str(uuid.uuid4())
    )


def recover(home, run_id, destination, worker=None):
    from .workspace import export_delta, snapshot

    run = run_path(home, run_id)
    board = Board(run / "state.sqlite")
    # An immutable candidate can be exported during activity. A live mutable worker cannot.
    candidate = board.meta("candidate")
    if worker is None and candidate:
        manifest = candidate["manifest"]
    else:
        if worker not in ("a", "b"):
            raise HiveError("worker_required", "Select retained worker a or b")
        if status(home, run_id)["supervisor_alive"]:
            raise HiveError("still_running", "Stop runtime before exporting a mutable workspace")
        for p in run.glob("process-*.json"):
            record = read_json(p)
            if any(alive(r) for r in [record["parent"], *record["descendants"]] if r):
                raise HiveError("shutdown_unconfirmed", "Retained writer is still alive")
        manifest = snapshot(run / "workers" / worker, run)["files"]
    destination = Path(destination).expanduser().resolve()
    source = Path(read_json(run / "config.json")["project"])
    if destination.is_relative_to(source) or source.is_relative_to(destination):
        raise HiveError("unsafe_export", "Recovery destination must not overlap original project")
    for path in [run / "workers", run / "objects", run / "baseline"]:
        if destination.is_relative_to(path) or path.is_relative_to(destination):
            raise HiveError("unsafe_export", "Recovery must not overlap runtime inputs")
    value = export_delta(run, manifest, destination)
    return {"destination": str(destination), **value}
