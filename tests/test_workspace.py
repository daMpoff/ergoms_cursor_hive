import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from cursor_hive_runtime.errors import HiveError
from cursor_hive_runtime.supervisor import verify_candidate
from cursor_hive_runtime.util import read_json
from cursor_hive_runtime.workspace import (
    apply_publication,
    capture,
    export_delta,
    prepare,
    publication_snapshot,
    snapshot,
)


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project, self.run = self.root / "проект с пробелом", self.root / "run"
        self.project.mkdir()
        self.run.mkdir()
        subprocess.run(["git", "init", "--quiet", str(self.project)], check=True)
        (self.project / "tracked.txt").write_text("staged", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "tracked.txt"], check=True)
        (self.project / "tracked.txt").write_text("unstaged", encoding="utf-8")
        (self.project / "untracked.txt").write_text("untracked", encoding="utf-8")
        (self.project / ".env").write_text("secret", encoding="utf-8")
        self.index = (self.project / ".git/index").read_bytes()
        self.base = capture(self.project, self.run)
        prepare(self.run, self.base)

    def test_dirty_state_and_index_preserved(self):
        self.assertEqual((self.run / "workers/a/tracked.txt").read_text(), "unstaged")
        self.assertIn("untracked.txt", self.base)
        self.assertNotIn(".env", self.base)
        self.assertEqual((self.project / ".git/index").read_bytes(), self.index)

    def test_publication_frozen_and_conflicting_import_has_no_partial_writes(self):
        a, b = self.run / "workers/a", self.run / "workers/b"
        (a / "tracked.txt").write_text("published")
        (a / "new.txt").write_text("new")
        manifest = publication_snapshot(self.run, a, ["tracked.txt", "new.txt"])
        (a / "tracked.txt").write_text("later")
        (b / "tracked.txt").write_text("own edit")
        pub = {"id": "pub", "manifest": manifest}
        with self.assertRaises(HiveError):
            apply_publication(self.run, b, pub, "op")
        self.assertFalse((b / "new.txt").exists())
        (b / "tracked.txt").write_text("unstaged")
        apply_publication(self.run, b, pub, "op")
        self.assertEqual((b / "tracked.txt").read_text(), "published")
        apply_publication(self.run, b, pub, "op")

    def test_export_idempotent_and_source_unchanged(self):
        a = self.run / "workers/a"
        (a / "tracked.txt").write_text("result")
        (a / "untracked.txt").unlink()
        result = snapshot(a, self.run)["files"]
        target = self.root / "export"
        first = export_delta(self.run, result, target)
        self.assertEqual(export_delta(self.run, result, target), first)
        self.assertEqual((self.project / "tracked.txt").read_text(), "unstaged")
        self.assertIn("untracked.txt", read_json(target / "manifest.json")["deleted"])
        self.assertEqual((self.project / ".git/index").read_bytes(), self.index)

    def test_traversal_and_symlink_rejected(self):
        a = self.run / "workers/a"
        with self.assertRaises(HiveError):
            publication_snapshot(self.run, a, ["../outside"])
        if sys.platform != "win32":
            (a / "link").symlink_to(self.project / "tracked.txt")
            with self.assertRaises(HiveError):
                publication_snapshot(self.run, a, ["link"])

    def test_check_mutating_inputs_does_not_pass(self):
        command = [
            sys.executable,
            "-c",
            "from pathlib import Path; Path('tracked.txt').write_text('changed')",
        ]
        receipt = verify_candidate(self.run, self.base, command, 5)
        self.assertEqual(receipt["exit_code"], 0)
        self.assertFalse(receipt["inputs_unchanged"])
        self.assertFalse(receipt["passed"])


if __name__ == "__main__":
    unittest.main()
