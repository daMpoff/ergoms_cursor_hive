# CursorHive development

This is a standalone Python 3.12 project. ERGO MS is optional.

- Read BEHAVIOR.md and docs/IMPLEMENTATION_STATUS.md before changing behavior.
- Keep the source project and its Git index unchanged. Export is the only delivery backend in 0.1.
- Treat host qualification as separate from fake-agent tests. Never claim live Cursor was tested without executing it.
- Do not turn CLI exit status or model text into a check receipt.
- Bind worker identity to the connection, not tool arguments. Do not pass controller credentials to workers.
- Use SQL transactions for board mutations and their events; retain operation idempotency.
- Never automatically steal claims from a live process or delete retained workspaces.
- Run `.venv/bin/python -m unittest discover -s tests -v` and `.venv/bin/ruff check .` (use Windows equivalents there).
- Update implementation status when a feature or limitation changes.
