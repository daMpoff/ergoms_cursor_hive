---
name: hive-run
description: Start and observe a CursorHive shared-board run when the user explicitly asks for Hive collaboration.
---

Use Hive only for an explicit request. The 0.1 Cursor adapter is experimental; automatic delivery and exact IDE chat cloning are unavailable.

1. Determine the explicit Git root and complete task text; do not claim native capture of the prompt.
2. Call hive_run_start with a stable operation_id for retries. Default experimental_cursor is false, so an unqualified host fails clearly. Experimental mode is a visible user-selected configuration, not an automatic fallback. Never secretly set force=true.
3. Keep the returned run_id. Call hive_run_status for observation, without editing the same original source while workers run. Poll only when the user requests status or a bounded follow-up is useful; never hold one MCP call for the whole run.
4. For changes use hive_run_update with the complete replacement request and expected_revision. An accepted update may still await worker acknowledgement.
5. For stop use hive_run_stop and confirm the resulting terminal state. Closing chat is not cancellation.
6. Present the actual export path, changed files, check results and limitations. Exported files have not been applied to the original project. ready=false must not be presented as production readiness.
7. For recovery choose a new directory outside source and runtime inputs using hive_recover_export. Do not delete retained worker trees.

Worker invocations must use their worker-bound tools. Never fall back to controller mode if a worker binding is invalid.
