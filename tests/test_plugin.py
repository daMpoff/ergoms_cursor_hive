import asyncio
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PluginTests(unittest.TestCase):
    def test_built_and_installed_plugin_starts_mcp_without_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = load_script("build_plugin").build(root / "package")
            result = load_script("install_plugin").install(
                package, root / "local", runtime_python=sys.executable
            )
            launcher = Path(result["plugin"]) / "bin/launcher.py"
            env = {k: v for k, v in os.environ.items() if not k.startswith("CURSOR_HIVE_")}
            env.pop("PYTHONPATH", None)

            # -S is not used: dependencies legitimately live in the runtime environment.
            async def scenario():
                params = StdioServerParameters(
                    command=sys.executable, args=[str(launcher)], env=env, cwd=str(root)
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        tools = await session.list_tools()
                        self.assertIn("hive_run_start", {t.name for t in tools.tools})

            asyncio.run(scenario())
            # Prove the bundled source is importable from a child, not just sys.path in launcher.
            probe = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import cursor_hive_runtime; print(cursor_hive_runtime.__file__)",
                ],
                cwd=root,
                env={**env, "PYTHONPATH": str(package / "runtime/src")},
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertIn(str(package), probe.stdout)

    def test_installer_refuses_unowned_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package = load_script("build_plugin").build(root / "package")
            target = root / "local/cursor-hive"
            target.mkdir(parents=True)
            (target / "user.txt").write_text("preserve")
            with self.assertRaises(ValueError):
                load_script("install_plugin").install(package, root / "local", sys.executable)
            self.assertEqual((target / "user.txt").read_text(), "preserve")


if __name__ == "__main__":
    unittest.main()
