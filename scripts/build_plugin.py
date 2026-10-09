"""Build a resource-complete plugin, independent of the source checkout."""

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(destination=None):
    target = Path(destination) if destination else ROOT / "dist/cursor-hive"
    if target.exists():
        raise FileExistsError(f"Build into a new directory: {target}")
    shutil.copytree(ROOT / "hosts/cursor", target)
    source = target / "runtime/src/cursor_hive_runtime"
    shutil.copytree(
        ROOT / "src/cursor_hive_runtime",
        source,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copyfile(ROOT / "requirements.lock", target / "runtime/requirements.lock")
    shutil.copyfile(ROOT / "LICENSE", target / "LICENSE")
    shutil.copyfile(ROOT / "THIRD_PARTY_NOTICES.md", target / "THIRD_PARTY_NOTICES.md")
    if (ROOT / "dist").exists():
        wheels = list((ROOT / "dist").glob("cursor_hive-*.whl"))
        if wheels:
            (target / "runtime/wheels").mkdir()
            for wheel in wheels:
                shutil.copyfile(wheel, target / "runtime/wheels" / wheel.name)
    manifest = json.loads((target / ".cursor-plugin/plugin.json").read_text())
    assert manifest["name"] == "cursor-hive"
    return target.resolve()


if __name__ == "__main__":
    print(build())
