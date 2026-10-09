"""Conservative UTF-8 snapshots and export-only delivery."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import HiveError
from .util import atomic_json, contained, digest, read_json

MAX_FILE = 2 * 1024 * 1024
MAX_TREE = 32 * 1024 * 1024
RESERVED = {".cursor/mcp.json", ".cursor/cli.json"}
EXCLUDED_PARTS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".hive-state"}


def excluded(name):
    name = name.casefold()
    parts = name.split("/")
    leaf = parts[-1]
    return (
        name in RESERVED
        or any(p in EXCLUDED_PARTS for p in parts)
        or leaf.startswith(".env")
        or leaf in {".npmrc", ".netrc", ".pypirc"}
        or leaf.lower().endswith((".pem", ".key", ".p12", ".pfx"))
    )


def git(root, *args):
    try:
        p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise HiveError("git_error", str(e)) from e
    if p.returncode:
        raise HiveError("git_error", p.stderr.decode(errors="replace")[:1500])
    return p.stdout


def git_names(root):
    return sorted(
        set(
            os.fsdecode(n)
            for n in git(
                root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"
            ).split(b"\0")
            if n
        )
    )


def put_object(run, data):
    key = digest(data)
    path = run / "objects" / key
    path.parent.mkdir(exist_ok=True)
    if path.exists():
        if digest(path.read_bytes()) != key:
            raise HiveError("corrupt_object", key)
    else:
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    return key


def object_bytes(run, key):
    if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise HiveError("invalid_object", "Invalid hash")
    data = (run / "objects" / key).read_bytes()
    if digest(data) != key:
        raise HiveError("corrupt_object", key)
    return data


def snapshot(root, run, names=None):
    names = git_names(root) if names is None else sorted(set(names))
    manifest, skips, total, folded = {}, [], 0, set()
    for name in names:
        if excluded(name):
            skips.append(name)
            continue
        path = contained(root, name)
        if not path.exists():
            continue  # Tracked working-tree deletion is represented by absence.
        if not path.is_file():
            raise HiveError("unsupported_file", name)
        if name.casefold() in folded:
            raise HiveError("path_collision", name)
        folded.add(name.casefold())
        st = path.stat()
        if st.st_size > MAX_FILE:
            raise HiveError("file_too_large", name)
        data = path.read_bytes()
        if len(data) > MAX_FILE:
            raise HiveError("file_too_large", name)
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HiveError("unsupported_binary", name) from e
        if b"\0" in data:
            raise HiveError("unsupported_binary", name)
        if st.st_mode & 0o111:
            raise HiveError("unsupported_mode", f"Executable mode: {name}")
        total += len(data)
        if total > MAX_TREE:
            raise HiveError("tree_too_large", "MVP snapshot exceeds 32 MiB")
        manifest[name] = put_object(run, data)
    return {"files": manifest, "excluded": skips}


def materialize(run, manifest, destination):
    if destination.exists():
        raise HiveError("destination_exists", str(destination))
    destination.mkdir(parents=True)
    for name, key in manifest.items():
        path = contained(destination, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(object_bytes(run, key))


def capture(project, run):
    top = Path(os.fsdecode(git(project, "rev-parse", "--show-toplevel")).strip()).resolve()
    if top != project.resolve():
        raise HiveError("wrong_root", f"Select the Git root: {top}")
    staged = git(project, "ls-files", "--stage", "-z")
    if any(entry.startswith(b"100755 ") for entry in staged.split(b"\0")):
        raise HiveError("unsupported_mode", "Executable Git modes require a future backend")
    if any(entry.startswith(b"160000 ") for entry in staged.split(b"\0")):
        raise HiveError("unsupported_submodule", "Submodules require a future backend")
    attrs = project / ".gitattributes"
    if attrs.exists() and b"filter=lfs" in attrs.read_bytes():
        raise HiveError("unsupported_lfs", "Git LFS capture is not implemented")
    index_name = Path(os.fsdecode(git(project, "rev-parse", "--git-path", "index")).strip())
    index = index_name if index_name.is_absolute() else project / index_name
    before_index = digest(index.read_bytes()) if index.exists() else None
    first = snapshot(project, run)
    second = snapshot(project, run)
    after_index = digest(index.read_bytes()) if index.exists() else None
    if first != second or before_index != after_index:
        raise HiveError("unstable_capture", "Source files or index changed while capturing")
    atomic_json(run / "baseline.json", {**first, "index_hash": before_index})
    return first["files"]


def prepare(run, manifest):
    for actor in ("a", "b"):
        workspace = run / "workers" / actor
        materialize(run, manifest, workspace)
        git(workspace, "init", "--quiet")
        # No commit, hooks, user identity or source Git configuration is required.
        git(workspace, "add", "--all")


def publication_snapshot(run, workspace, paths):
    if not isinstance(paths, list) or len(paths) > 64 or any(not isinstance(p, str) for p in paths):
        raise HiveError("invalid_paths", "At most 64 relative file paths are allowed")
    base = read_json(run / "baseline.json")["files"]
    snap = snapshot(workspace, run, paths)
    if snap["excluded"] or len(snap["files"]) != len(set(paths)):
        raise HiveError("excluded_path", "Publication includes reserved or missing paths")
    return {name: {"base": base.get(name), "result": key} for name, key in snap["files"].items()}


def apply_publication(run, workspace, publication, op):
    files = publication["manifest"]
    journal = run / "imports" / f"{digest(op.encode())}.json"
    conflicts, writes = [], []
    for name, record in files.items():
        path = contained(workspace, name)
        if path.exists() and not path.is_file():
            conflicts.append(name)
            continue
        old = digest(path.read_bytes()) if path.exists() else None
        if old == record["result"]:
            continue
        if old != record["base"]:
            conflicts.append(name)
        else:
            writes.append((name, path, object_bytes(run, record["result"])))
    if conflicts:
        raise HiveError("import_conflict", ", ".join(conflicts))
    atomic_json(
        journal,
        {"publication": publication["id"], "paths": [w[0] for w in writes], "state": "applying"},
    )
    for _, path, data in writes:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)
    atomic_json(journal, {"publication": publication["id"], "state": "completed"})
    return {"applied": [w[0] for w in writes]}


def export_delta(run, manifest, destination):
    base = read_json(run / "baseline.json")["files"]
    changed = {p: h for p, h in manifest.items() if base.get(p) != h}
    removed = sorted(set(base) - set(manifest))
    contract = {
        "format": 1,
        "changed": {p: {"base": base.get(p), "result": h} for p, h in changed.items()},
        "deleted": removed,
        "apply_automatically": False,
    }
    if destination.exists():
        if (destination / "manifest.json").exists() and read_json(
            destination / "manifest.json"
        ) == contract:
            for p, h in changed.items():
                if digest(contained(destination / "files", p).read_bytes()) != h:
                    raise HiveError("export_corrupt", p)
            for p in sorted(set(changed) | set(removed)):
                if (
                    p in base
                    and digest(contained(destination / "bases", p).read_bytes()) != base[p]
                ):
                    raise HiveError("export_corrupt", p)
            return contract
        raise HiveError("destination_exists", str(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".hive-export-", dir=destination.parent))
    try:
        materialize(run, changed, staging / "files")
        materialize(
            run,
            {p: base[p] for p in changed if p in base} | {p: base[p] for p in removed},
            staging / "bases",
        )
        # mkdir is a no-replace reservation; rename alone can replace a concurrent empty directory.
        try:
            destination.mkdir()
        except FileExistsError as e:
            raise HiveError("destination_exists", str(destination)) from e
        atomic_json(destination / "incomplete.json", {"format": 1, "state": "publishing"})
        os.rename(staging / "files", destination / "files")
        os.rename(staging / "bases", destination / "bases")
        atomic_json(destination / "manifest.json", contract)
        (destination / "incomplete.json").unlink()
        staging.rmdir()
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise
    return contract
