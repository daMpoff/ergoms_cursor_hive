"""Plugin resources are self-contained. Dependencies live in the installed environment."""

import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "runtime/src"))
os.environ["PYTHONPATH"] = str(root / "runtime/src") + (
    os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""
)

from cursor_hive_runtime.cli import main  # noqa: E402

sys.argv = ["cursor-hive", "mcp"]
main()
