# Cursor compatibility

No installed real Cursor CLI is present in the development environment. `doctor` reports this explicitly and spends no model quota.

| Component | Verified here |
| --- | --- |
| Python 3.12 / Linux | Runtime and local tests |
| MCP SDK 1.30.0 | Real stdio initialization, inventory and tool errors |
| Cursor headless | Mock CLI transport, argument and terminal-result tests |
| Cursor IDE plugin | Resource assembly and installed launcher; no IDE validation |
| Windows native Cursor | Not run |
| ACP / VSIX | Not implemented |

The adapter uses documented print/stream-json/workspace/resume/model/force flags and MCP enable, based on [Cursor parameters](https://cursor.com/docs/cli/reference/parameters) and [CLI MCP](https://cursor.com/docs/cli/mcp). The plugin follows [Cursor Plugins](https://cursor.com/docs/plugins) and the [reference](https://cursor.com/docs/reference/plugins).

The host's native permissions and MCP authorization still apply. Effective project/global policy is read before preparing copies; no empty deny is injected to replace global restrictions. Worker configs are generated inside private copies only. Global skills/tools are not promised to match a live IDE session.

MCP is intentionally pinned to the supported 1.x SDK maintenance line. The current SDK main documentation describes v2; this implementation uses [official v1.x FastMCP](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x) and a <2 bound. Upgrading requires a separate migration and transport test.
