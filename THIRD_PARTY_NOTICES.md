# Sources and dependencies

CursorHive's design is informed by [DeLM](https://arxiv.org/abs/2606.10662) and
[delm-agent-plugin](https://github.com/Decentralized-LM/delm-agent-plugin), studied
at commit `3bd42f9199cfef676e8b17c048346af1bbeb338e`. No native adapter source files
from that repository are copied in this initial implementation.

Runtime dependencies include the official [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
(MIT) and [psutil](https://github.com/giampaolo/psutil) (BSD-3-Clause). Their packages
and license notices remain in the managed Python environment. The complete resolved
dependency versions are recorded in requirements.lock; dependencies keep their own licenses.

Cursor CLI is an external prerequisite, distributed by Cursor under its own terms.
