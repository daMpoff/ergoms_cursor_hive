import hashlib
import json
import os
import tempfile
from pathlib import Path

from .errors import HiveError


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(data: bytes):
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(canonical(data))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def contained(root: Path, name: str):
    # Reject Windows drive/ADS names on every OS, not only Windows.
    if not isinstance(name, str) or not name or "\\" in name or ":" in name:
        raise HiveError("invalid_path", "Expected a portable relative path")
    parts = name.split("/")
    if name.startswith("/") or any(p in ("", ".", "..") or p.casefold() == ".git" for p in parts):
        raise HiveError("invalid_path", "Path escapes the admitted project")
    for p in parts:
        if any(c in '<>"|?*' or ord(c) < 32 for c in p):
            raise HiveError("invalid_path", "Unsupported portable filename")
        if p.endswith((" ", ".")) or p.split(".")[0].upper() in {
            "CON",
            "PRN",
            "AUX",
            "NUL",
            *[f"COM{i}" for i in range(1, 10)],
            *[f"LPT{i}" for i in range(1, 10)],
        }:
            raise HiveError("invalid_path", "Unsupported Windows filename")
    path = root.joinpath(*parts)
    for ancestor in [path, *path.parents]:
        if ancestor == root.parent:
            break
        if ancestor.is_symlink():
            raise HiveError("invalid_path", "Symlinks are unsupported")
    if not path.resolve().is_relative_to(root.resolve()):
        raise HiveError("invalid_path", "Path is outside workspace")
    return path


def state_home(value=None):
    if value:
        return Path(value).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_STATE_HOME")
    return (
        Path(base) / "cursor-hive" if base else Path.home() / ".local/state/cursor-hive"
    ).resolve()
