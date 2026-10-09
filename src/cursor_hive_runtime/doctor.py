import importlib.metadata
import os
import platform
import shutil
import sqlite3
import subprocess
import sys

from . import __version__


def doctor(cursor=None):
    executable = cursor or shutil.which("agent") or shutil.which("cursor-agent")
    version, error = None, None
    if executable:
        try:
            p = subprocess.run(
                [executable, "--version"], capture_output=True, timeout=10, text=True
            )
            version = p.stdout.strip() if p.returncode == 0 else None
            if p.returncode:
                error = "Cursor --version failed"
        except (OSError, subprocess.TimeoutExpired) as e:
            error = str(e)
    try:
        sdk = importlib.metadata.version("mcp")
    except importlib.metadata.PackageNotFoundError:
        sdk = None
    return {
        "runtime_version": __version__,
        "python": sys.version.split()[0],
        "os": platform.system(),
        "sqlite": sqlite3.sqlite_version,
        "git": shutil.which("git"),
        "cursor": executable,
        "cursor_version": version,
        "cursor_error": error,
        "mcp_sdk": sdk,
        "local_prerequisites": bool(shutil.which("git") and sdk),
        "cursor_prerequisites": bool(version and sdk),
        "live_qualified": False,
        "live_checks": {
            k: "not_run"
            for k in (
                "write_policy",
                "deny_policy",
                "mcp_identity",
                "resume",
                "cancellation",
                "windows_descendants",
            )
        },
        "model_calls": 0,
        "worker_binding_present": bool(os.environ.get("CURSOR_HIVE_WORKER")),
    }
