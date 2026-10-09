"""Retain handles to owned children; never infer kill authority from cwd."""

import os
import signal
import subprocess
import time
from pathlib import Path

import psutil

from .errors import HiveError
from .util import atomic_json, digest, read_json


def identity(pid):
    try:
        if os.name != "nt":
            os.kill(pid, 0)
        p = psutil.Process(pid)
        return {"pid": pid, "created": p.create_time()}
    except ProcessLookupError:
        return None
    except psutil.Error as error:
        # Some containers cannot inspect /proc for every real child. Unknown creation
        # time is never authorization to kill a saved PID. A live Popen handle is.
        if os.name != "nt":
            try:
                os.kill(pid, 0)
                return {"pid": pid, "created": None}
            except ProcessLookupError:
                return None
        if isinstance(error, psutil.NoSuchProcess):
            return None
        return {"pid": pid, "created": None}


def alive(record):
    if not record:
        return False
    if record.get("stopped"):
        return False
    actual = identity(record["pid"])
    if actual is None:
        return False
    if record.get("created") is None or actual.get("created") is None:
        return True  # Conservative liveness, not kill authority.
    if actual != record:
        return False
    try:
        return psutil.Process(record["pid"]).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


class ProjectLock:
    """Persistent no-replace reservation. Only its recorded run may release it."""

    def __init__(self, home: Path, project: Path):
        normalized = os.path.normcase(str(project.resolve()))
        self.path = home / "locks" / f"{digest(normalized.encode())}.json"

    def acquire(self, run_id):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open("x", encoding="utf-8") as f:
                os.chmod(self.path, 0o600)
                import json

                json.dump({"run_id": run_id}, f)
        except FileExistsError as e:
            raise HiveError(
                "project_locked", f"Retained run: {read_json(self.path)['run_id']}"
            ) from e

    def release(self, run_id):
        if self.path.exists() and read_json(self.path).get("run_id") == run_id:
            self.path.unlink()


class OwnedProcess:
    def __init__(self, argv, *, cwd, env, stdout, stderr):
        kwargs = (
            {"start_new_session": True}
            if os.name != "nt"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        )
        self.child = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **kwargs
        )
        self.record = identity(self.child.pid)
        self.descendants = {}

    def observe(self):
        if self.record and alive(self.record):
            try:
                for p in psutil.Process(self.child.pid).children(recursive=True):
                    self.descendants[p.pid] = {"pid": p.pid, "created": p.create_time()}
            except psutil.Error:
                pass

    def stop(self):
        self.observe()
        records = list(self.descendants.values()) + ([self.record] if self.record else [])
        if os.name != "nt" and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        for r in records:
            if r.get("created") is not None and alive(r):
                try:
                    psutil.Process(r["pid"]).terminate()
                except psutil.NoSuchProcess:
                    pass
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(alive(r) for r in records):
            self.child.poll()
            time.sleep(0.025)
        for r in records:
            if r.get("created") is not None and alive(r):
                try:
                    psutil.Process(r["pid"]).kill()
                except psutil.NoSuchProcess:
                    pass
        if self.child.poll() is None:
            self.child.terminate()  # Native Popen ownership, including Windows handle.
            try:
                self.child.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.child.kill()
        try:
            self.child.wait(timeout=3)
        except subprocess.TimeoutExpired as e:
            raise HiveError("shutdown_unconfirmed", "Owned process did not stop") from e
        if self.record:
            self.record["stopped"] = True
        # A zombie cannot write. psutil sees it until its parent reaps it.
        survivors = []
        for r in records:
            if alive(r):
                try:
                    if psutil.Process(r["pid"]).status() != psutil.STATUS_ZOMBIE:
                        survivors.append(r)
                except psutil.NoSuchProcess:
                    pass
        if survivors:
            raise HiveError("shutdown_unconfirmed", "Retained descendants may still write")

    def save(self, path):
        self.observe()
        atomic_json(path, {"parent": self.record, "descendants": list(self.descendants.values())})
