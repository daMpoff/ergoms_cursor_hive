import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from cursor_hive_runtime.board import Board
from cursor_hive_runtime.errors import HiveError


class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.board = Board(Path(self.tmp.name) / "board.sqlite")
        self.board.initialize("run", "/project", "request")
        self.board.set_meta("state", "running")
        for actor in ("a", "b"):
            self.call(actor, "ack")

    def call(self, actor, method, **args):
        return self.board.mutate(actor, str(uuid.uuid4()), method, {"revision": 1, **args})

    def test_two_claimants_exactly_one_owner(self):
        task = self.call("a", "task_create", description="implement")
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.call, actor, "task_claim", **task) for actor in ("a", "b")]
        self.assertEqual(sum(bool(f.result()["task_id"]) for f in futures), 1)

    def test_duplicate_retry_and_argument_conflict(self):
        args = {"revision": 1, "description": "work"}
        first = self.board.mutate("a", "repeat", "task_create", args)
        self.assertEqual(first, self.board.mutate("a", "repeat", "task_create", args))
        self.assertEqual(len(self.board.context()["tasks"]), 1)
        with self.assertRaises(HiveError) as e:
            self.board.mutate("a", "repeat", "task_create", {**args, "description": "other"})
        self.assertEqual(e.exception.code, "operation_conflict")

    def test_stale_claim_cannot_complete_after_release_and_reassign(self):
        task = self.call("a", "task_create", description="work")
        claim = self.call("a", "task_claim", **task)
        self.call(
            "a", "task_release", task_id=claim["task_id"], claim_version=claim["claim_version"]
        )
        self.call("b", "task_claim", **task)
        pub = self.call("a", "publish", kind="completion", message="late")
        with self.assertRaises(HiveError) as e:
            self.call(
                "a",
                "task_complete",
                task_id=claim["task_id"],
                claim_version=claim["claim_version"],
                **pub,
            )
        self.assertEqual(e.exception.code, "stale_claim")

    def test_revision_invalidates_selected_candidate(self):
        task = self.call("a", "task_create", description="work")
        claim = self.call("a", "task_claim", **task)
        pub = self.call("a", "publish", kind="completion", message="done")
        self.call(
            "a",
            "task_complete",
            task_id=claim["task_id"],
            claim_version=claim["claim_version"],
            **pub,
        )
        self.call("a", "integration_claim")
        self.call("a", "candidate", summary="satisfied")
        self.board.mutate(
            "controller", "update", "update", {"request": "new", "expected_revision": 1}
        )
        self.assertIsNone(self.board.close_admission())
        with self.assertRaises(HiveError):
            self.call("a", "task_create", description="old")

    def test_finishing_closes_update_admission(self):
        self.board.set_meta("candidate", {"id": "c", "revision": 1})
        self.assertIsNotNone(self.board.close_admission())
        with self.assertRaises(HiveError) as e:
            self.board.mutate(
                "controller", "u", "update", {"request": "new", "expected_revision": 1}
            )
        self.assertEqual(e.exception.code, "update_closed")

    def test_context_pagination_has_no_gaps(self):
        for i in range(4):
            self.call("a", "publish", kind="fact", message=str(i))
        seen, cursor = [], 0
        while True:
            page = self.board.context(cursor, limit=2)
            seen += [e["sequence"] for e in page["events"]]
            cursor = page["cursor"]
            if not page["has_more"]:
                break
        self.assertEqual(seen, list(range(1, cursor + 1)))

    def test_persistence_across_reopen(self):
        task = self.call("a", "task_create", description="durable")
        restored = Board(self.board.path)
        self.assertEqual(restored.context()["tasks"][0]["id"], task["task_id"])


if __name__ == "__main__":
    unittest.main()
