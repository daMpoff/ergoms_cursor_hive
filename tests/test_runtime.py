import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from cursor_hive_runtime.adapters import cursor_argv, parse_stream
from cursor_hive_runtime.board import Board
from cursor_hive_runtime.cli import wait
from cursor_hive_runtime.controller import control, recover, start_run, status
from cursor_hive_runtime.errors import HiveError
from cursor_hive_runtime.mcp_server import make_server
from cursor_hive_runtime.processes import OwnedProcess, alive, identity
from cursor_hive_runtime.rpc import Client, RuntimeAPI, start_server
from cursor_hive_runtime.supervisor import Supervisor, should_wake
from cursor_hive_runtime.util import atomic_json, read_json


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "source space"
        self.project.mkdir()
        self.home = self.root / "state"
        subprocess.run(["git", "init", "--quiet", str(self.project)], check=True)
        (self.project / "source.txt").write_text("staged", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.project), "add", "source.txt"], check=True)
        (self.project / "source.txt").write_text("dirty", encoding="utf-8")
        self.index = (self.project / ".git/index").read_bytes()
        self.addCleanup(self.stop_runs)

    def stop_runs(self):
        for run in (self.home / "runs").glob("*"):
            process = run / "supervisor.json"
            if process.exists() and alive(read_json(process)):
                try:
                    control(self.home, run.name, "stop")
                except HiveError:
                    pass
                self.wait_for_exit(run.name)

    def wait_for_exit(self, run_id):
        process = self.home / "runs" / run_id / "supervisor.json"
        record = read_json(process)
        end = time.monotonic() + 20
        while time.monotonic() < end and alive(record):
            time.sleep(0.05)
        self.assertFalse(alive(record), "Supervisor must exit before removing retained files")

    def test_full_two_process_demo_exports_peer_result_without_source_edits(self):
        result = start_run(
            self.project, "demo", home=self.home, operation_id="fixed", backend="fake"
        )
        run_id = result["run_id"]
        repeated = start_run(
            self.project, "demo", home=self.home, operation_id="fixed", backend="fake"
        )
        self.assertEqual(run_id, repeated["run_id"])
        end = wait(self.home, run_id, 45)
        self.wait_for_exit(run_id)
        self.assertEqual(end["state"], "exported", end.get("error"))
        report = read_json(self.home / "runs" / run_id / "report.json")
        self.assertEqual(report["turns"], {"a": 1, "b": 1})
        exported = Path(report["export"]) / "files/hive-demo-b.txt"
        self.assertIn("Reused by fake agent B", exported.read_text())
        self.assertEqual((self.project / "source.txt").read_text(), "dirty")
        self.assertEqual((self.project / ".git/index").read_bytes(), self.index)
        self.assertFalse((self.project / "hive-demo-a.txt").exists())
        recovery = self.root / "recovered"
        recover(self.home, run_id, recovery)
        self.assertEqual(
            recover(self.home, run_id, recovery)["changed"],
            read_json(recovery / "manifest.json")["changed"],
        )

    def test_candidate_check_exit_observed_without_claiming_original_ready(self):
        result = start_run(
            self.project,
            "demo",
            home=self.home,
            backend="fake",
            check_argv=[
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('hive-demo-b.txt').exists()",
            ],
        )
        end = wait(self.home, result["run_id"], 45)
        self.wait_for_exit(result["run_id"])
        self.assertEqual(end["state"], "exported", end.get("error"))
        self.assertTrue(end["report"]["checks"]["observed"])
        self.assertTrue(end["report"]["checks"]["passed"])
        self.assertFalse(end["report"]["ready"])

    def test_nested_worker_start_rejected(self):
        with patch.dict(os.environ, {"CURSOR_HIVE_WORKER": "a"}):
            with self.assertRaises(HiveError) as e:
                start_run(self.project, "nested", home=self.home, backend="fake")
        self.assertEqual(e.exception.code, "nested_start")

    def test_missing_pid_is_not_reported_alive(self):
        self.assertIsNone(identity(2**30))
        self.assertFalse(alive({"pid": 2**30, "created": None}))

    def test_worker_principal_cannot_stop_or_impersonate_controller(self):
        self.home.mkdir()
        board = Board(self.home / "board.sqlite")
        board.initialize("run", self.project, "request")
        board.set_meta("state", "running")
        api = RuntimeAPI(board, self.home, "controller-secret")
        api.bind("a", "worker-secret")
        server, url = start_server(api)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = Client(url, "worker-secret")
        with self.assertRaises(HiveError) as e:
            client.call("stop", {}, "attempt")
        self.assertEqual(e.exception.code, "forbidden")
        with self.assertRaises(HiveError) as e:
            Client(url, "wrong").call("context")
        self.assertEqual(e.exception.code, "unauthorized")
        api.bind("a", "new-secret")
        with self.assertRaises(HiveError):
            client.call("context")

    def test_old_turn_cannot_acknowledge_new_revision(self):
        self.home.mkdir()
        board = Board(self.home / "board.sqlite")
        board.initialize("run", self.project, "request")
        board.set_meta("state", "running")
        api = RuntimeAPI(board, self.home, "controller")
        api.bind("a", "old-turn")
        server, url = start_server(api)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        Client(url, "controller").call(
            "update", {"expected_revision": 1, "request": "revised"}, "u"
        )
        with self.assertRaises(HiveError) as e:
            Client(url, "old-turn").call("ack", {"revision": 2}, "ack")
        self.assertEqual(e.exception.code, "stale_turn")

    def test_stop_during_check_preserves_candidate_and_cancels_export(self):
        result = start_run(
            self.project,
            "demo",
            home=self.home,
            backend="fake",
            check_argv=[sys.executable, "-c", "import time; time.sleep(60)"],
            check_timeout=90,
        )
        run_id = result["run_id"]
        run = self.home / "runs" / run_id
        end = time.monotonic() + 30
        while time.monotonic() < end and not (run / "process-check.json").exists():
            time.sleep(0.025)
        self.assertTrue((run / "process-check.json").exists(), status(self.home, run_id))
        control(self.home, run_id, "stop")
        value = wait(self.home, run_id, 30)
        self.wait_for_exit(run_id)
        self.assertEqual(value["state"], "cancelled", value.get("error"))
        self.assertTrue(read_json(run / "check-receipt.json")["cancelled"])
        self.assertFalse((run / "export").exists())
        recover(self.home, run_id, self.root / "recovered-cancelled")

    @unittest.skipIf(
        os.name == "nt", "POSIX mock executable; Windows host needs live qualification"
    )
    def test_cursor_adapter_against_explicit_mock_cli(self):
        mock = self.root / "agent"
        mock.write_text(
            f"#!{sys.executable}\n" + "import os,sys,json\n"
            "if '--version' in sys.argv: print('test-double');sys.exit(0)\n"
            "if 'mcp' in sys.argv: sys.exit(0)\n"
            "actor=os.environ['CURSOR_HIVE_WORKER']\n"
            "session='mock-'+actor\n"
            "print(json.dumps({'type':'system','session_id':session}),flush=True)\n"
            "from cursor_hive_runtime.fake_agent import main\n"
            "if '--resume' in sys.argv:\n"
            " from cursor_hive_runtime.rpc import Client\n"
            " c=Client(os.environ['CURSOR_HIVE_URL'],os.environ['CURSOR_HIVE_TOKEN'])\n"
            " r=c.call('context')['revision']\n"
            " import uuid\n"
            " c.call('ack',{'revision':r},str(uuid.uuid4()))\n"
            " c.call('yield',{'revision':r},str(uuid.uuid4()))\n"
            "else: main()\n"
            "print(json.dumps({'type':'result','subtype':'success','session_id':session}),flush=True)\n"
        )
        mock.chmod(0o700)
        result = start_run(
            self.project,
            "mock-only",
            home=self.home,
            cursor=str(mock),
            experimental_cursor=True,
            timeout=15,
        )
        value = wait(self.home, result["run_id"], 20)
        self.wait_for_exit(result["run_id"])
        self.assertEqual(value["state"], "exported", value.get("error"))
        self.assertEqual(value["report"]["backend"], "cursor")

    def test_yield_does_not_wake_itself_but_peer_event_does(self):
        self.home.mkdir()
        board = Board(self.home / "board.sqlite")
        board.initialize("run", self.project, "request")
        board.set_meta("state", "running")
        for actor in ("a", "b"):
            board.mutate(actor, "ack", "ack", {"revision": 1})
        board.mutate("a", "yield", "yield", {"revision": 1})
        self.assertFalse(should_wake(board, "a"))
        board.mutate("b", "pub", "publish", {"revision": 1, "kind": "fact", "message": "peer"})
        self.assertTrue(should_wake(board, "a"))

    def test_shutdown_failure_retains_process_handle(self):
        self.home.mkdir()
        atomic_json(self.home / "config.json", {"controller_token": "secret", "backend": "fake"})
        board = Board(self.home / "state.sqlite")
        board.initialize("run", self.project, "request")
        supervisor = Supervisor(self.home)

        class BrokenProcess:
            def save(self, _):
                pass

            def stop(self):
                raise HiveError("shutdown_unconfirmed", "test")

        supervisor.processes["a"] = BrokenProcess()
        with self.assertRaises(HiveError):
            supervisor.stop_all()
        self.assertIn("a", supervisor.processes)

    def test_owned_process_stop_leaves_unrelated_process_alive(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        self.addCleanup(lambda: other.wait(timeout=5))
        self.addCleanup(other.terminate)
        record = identity(other.pid)
        with open(os.devnull, "wb") as out:
            owned = OwnedProcess(
                [sys.executable, "-c", "import time; time.sleep(20)"],
                cwd=self.project,
                env=os.environ.copy(),
                stdout=out,
                stderr=out,
            )
            owned.stop()
        self.assertTrue(alive(record))

    def test_native_argv_preserves_spaces_without_shell(self):
        argv = cursor_argv("agent", self.project, "a 'quoted' prompt", "session", "model", True)
        self.assertIn(str(self.project), argv)
        self.assertEqual(argv[-1], "a 'quoted' prompt")
        self.assertEqual(argv[argv.index("--resume") + 1], "session")

    def test_incomplete_stream_never_means_success(self):
        log = self.root / "stream"
        log.write_text(json.dumps({"type": "system", "session_id": "s"}) + "\n")
        with self.assertRaises(HiveError):
            parse_stream(log)
        log.write_text(
            log.read_text()
            + json.dumps({"type": "result", "subtype": "success", "session_id": "s"})
            + "\n"
        )
        self.assertEqual(parse_stream(log)["session_id"], "s")

    def test_worker_mcp_inventory_has_no_controller_tools(self):
        with patch.dict(
            os.environ,
            {
                "CURSOR_HIVE_WORKER": "a",
                "CURSOR_HIVE_URL": "http://127.0.0.1:1/rpc",
                "CURSOR_HIVE_TOKEN": "test",
            },
        ):
            tools = asyncio.run(make_server().list_tools())
        names = {t.name for t in tools}
        self.assertIn("hive_task_claim", names)
        self.assertNotIn("hive_run_start", names)

    def test_actual_mcp_stdio_handshake_and_controller_tool(self):
        async def scenario():
            env = {k: v for k, v in os.environ.items() if not k.startswith("CURSOR_HIVE_")}
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "cursor_hive_runtime", "--state-dir", str(self.home), "mcp"],
                env=env,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    self.assertIn("hive_run_start", names)
                    self.assertNotIn("hive_task_claim", names)
                    result = await session.call_tool(
                        "hive_run_status", {"run_id": str(uuid.uuid4())}
                    )
                    self.assertTrue(result.isError)

        asyncio.run(scenario())

    def test_worker_stdio_mcp_mutates_authenticated_board(self):
        self.home.mkdir()
        board = Board(self.home / "board.sqlite")
        board.initialize("run", self.project, "request")
        board.set_meta("state", "running")
        api = RuntimeAPI(board, self.home, "controller")
        api.bind("a", "worker")
        server, url = start_server(api)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        async def scenario():
            env = {
                **os.environ,
                "CURSOR_HIVE_WORKER": "a",
                "CURSOR_HIVE_URL": url,
                "CURSOR_HIVE_TOKEN": "worker",
            }
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "cursor_hive_runtime", "mcp"], env=env
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    ack = await session.call_tool(
                        "hive_request_ack", {"revision": 1, "operation_id": "ack"}
                    )
                    self.assertFalse(ack.isError)
                    result = await session.call_tool(
                        "hive_task_create",
                        {
                            "description": "native MCP transport",
                            "revision": 1,
                            "operation_id": "new",
                        },
                    )
                    self.assertFalse(result.isError)

        asyncio.run(scenario())
        self.assertEqual(board.context()["tasks"][0]["description"], "native MCP transport")


if __name__ == "__main__":
    unittest.main()
