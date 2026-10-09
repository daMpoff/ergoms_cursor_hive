import json
import os
import subprocess
import sys
from pathlib import Path

from .errors import HiveError
from .util import atomic_json, read_json


def permissions(project):
    user = Path.home() / ".cursor/cli-config.json"
    local = project / ".cursor/cli.json"
    global_config = read_json(user) if user.exists() else {}
    project_config = read_json(local) if local.exists() else {}
    policy = project_config.get("permissions", global_config.get("permissions", {}))
    if not isinstance(policy, dict):
        raise HiveError("invalid_permissions", "Invalid Cursor permissions")
    # Never inject an empty deny that erases effective global restrictions.
    return {
        "allow": list(policy.get("allow", [])) + ["Mcp(cursor-hive-worker:*)"],
        "deny": list(policy.get("deny", [])),
    }


def configure_worker(run, actor, url, token):
    workspace = run / "workers" / actor
    # The environment carries the secret, not project files or a prompt.
    atomic_json(
        workspace / ".cursor/mcp.json",
        {
            "mcpServers": {
                "cursor-hive-worker": {
                    "command": sys.executable,
                    "args": ["-m", "cursor_hive_runtime", "mcp"],
                    "env": {"CURSOR_HIVE_WORKER": actor},
                }
            }
        },
    )
    config = read_json(run / "config.json")
    atomic_json(workspace / ".cursor/cli.json", {"permissions": config["permissions"]})
    env = os.environ.copy()
    # Remove parent binding before assigning the only capability passed to this child.
    for key in list(env):
        if key.startswith("CURSOR_HIVE_"):
            del env[key]
    env.update({"CURSOR_HIVE_WORKER": actor, "CURSOR_HIVE_URL": url, "CURSOR_HIVE_TOKEN": token})
    return env


def cursor_argv(executable, workspace, prompt, session=None, model=None, force=False):
    argv = [
        executable,
        "-p",
        "--output-format",
        "stream-json",
        "--trust",
        "--workspace",
        str(workspace),
    ]
    if force:
        argv.append("--force")
    if model:
        argv += ["--model", model]
    if session:
        argv += ["--resume", session]
    return argv + [prompt]


def parse_stream(path):
    session, terminal, count = None, None, 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if len(line) > 2 * 1024 * 1024:
                raise HiveError("stream_limit", "Cursor event is too large")
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as e:
                raise HiveError("invalid_stream", "Cursor output is not NDJSON") from e
            if not isinstance(event, dict):
                raise HiveError("invalid_stream", "Expected an event object")
            count += 1
            if event.get("session_id"):
                if session and event["session_id"] != session:
                    raise HiveError("session_changed", "Mixed session IDs in one stream")
                session = event["session_id"]
            if event.get("type") == "result":
                terminal = event
    if (
        not session
        or not terminal
        or terminal.get("subtype") != "success"
        or terminal.get("is_error")
    ):
        raise HiveError("incomplete_turn", "Cursor did not provide a successful terminal result")
    return {"session_id": session, "events": count}


def peek_session(path):
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as stream:
        for line in stream.read(1024 * 1024).splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(event, dict)
                and event.get("type") == "system"
                and event.get("session_id")
            ):
                return event["session_id"]
    return None


def approve_worker_mcp(executable, workspace):
    result = subprocess.run(
        [executable, "--workspace", str(workspace), "mcp", "enable", "cursor-hive-worker"],
        capture_output=True,
        timeout=20,
    )
    if result.returncode:
        raise HiveError("mcp_approval_failed", "Could not enable the worker MCP in Cursor CLI")
