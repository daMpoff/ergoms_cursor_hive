"""Install only the owned local plugin; bootstrap dependencies once, outside the package."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import venv
from pathlib import Path


def install(package, local_root=None, runtime_python=None):
    package = Path(package).resolve()
    manifest = json.loads((package / ".cursor-plugin/plugin.json").read_text())
    if manifest["name"] != "cursor-hive":
        raise ValueError("Only CursorHive packages are supported")
    local_root = Path(local_root or Path.home() / ".cursor/plugins/local").resolve()
    target = local_root / "cursor-hive"
    if target.is_symlink() or (
        target.exists() and not (target / ".cursor-hive-owned.json").exists()
    ):
        raise ValueError("Existing destination is not owned by this installer")
    if runtime_python:
        # Resolving a venv symlink selects the global interpreter and loses its dependencies.
        executable = Path(runtime_python).expanduser().absolute()
    else:
        if sys.version_info[:2] != (3, 12):
            raise ValueError("Run the installer with Python 3.12")
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
        envdir = base / "cursor-hive" / "runtime" / manifest["version"]
        if not envdir.exists():
            venv.EnvBuilder(with_pip=True).create(envdir)
        executable = envdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        subprocess.run(
            [
                str(executable),
                "-m",
                "pip",
                "install",
                "-r",
                str(package / "runtime/requirements.lock"),
            ],
            check=True,
        )
    # Explicitly provided environments must still pass prerequisites.
    subprocess.run(
        [
            str(executable),
            "-c",
            "import mcp, psutil; import sys; assert sys.version_info[:2] == (3,12)",
        ],
        check=True,
    )
    local_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".cursor-hive-install-", dir=local_root))
    backup = None
    try:
        candidate = staging / "cursor-hive"
        shutil.copytree(package, candidate)
        config = json.loads((candidate / "mcp.json").read_text())
        config["mcpServers"]["cursor-hive"]["command"] = str(executable)
        (candidate / "mcp.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        (candidate / ".cursor-hive-owned.json").write_text(
            json.dumps({"version": manifest["version"]})
        )
        if target.exists():
            backup = local_root.parent / f"cursor-hive-backup-{uuid.uuid4()}"
            target.rename(backup)
        try:
            candidate.rename(target)
        except Exception:
            if backup:
                backup.rename(target)
            raise
    finally:
        shutil.rmtree(staging)
    return {
        "plugin": str(target),
        "runtime_python": str(executable),
        "backup": str(backup) if backup else None,
        "next": "Reload Cursor; verify plugin commands and MCP in Customize",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("package", nargs="?", default="dist/cursor-hive")
    parser.add_argument("--local-root")
    parser.add_argument("--runtime-python")
    args = parser.parse_args()
    print(json.dumps(install(args.package, args.local_root, args.runtime_python), indent=2))
