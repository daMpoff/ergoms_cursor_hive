"""Official MCP SDK v1 stdio transport; controller and worker have separate inventories."""

import os

from .controller import control, recover, start_run, status
from .errors import HiveError
from .rpc import Client


def make_server(home=None):
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as e:
        raise HiveError("mcp_missing", "Install cursor-hive[mcp] using the pinned lock") from e
    server = FastMCP("cursor-hive")
    actor = os.environ.get("CURSOR_HIVE_WORKER")
    if actor:
        if (
            actor not in ("a", "b")
            or not os.environ.get("CURSOR_HIVE_URL")
            or not os.environ.get("CURSOR_HIVE_TOKEN")
        ):
            raise HiveError("invalid_binding", "Incomplete worker binding; no controller fallback")
        client = Client(os.environ["CURSOR_HIVE_URL"], os.environ["CURSOR_HIVE_TOKEN"])

        def mutate(method, revision, operation_id, **args):
            return client.call(method, {"revision": revision, **args}, operation_id)

        @server.tool()
        def hive_context_read(after: int = 0, limit: int = 50) -> dict:
            """Read request, current tasks and new durable events. Continue while has_more."""
            return client.call("context", {"after": after, "limit": limit})

        @server.tool()
        def hive_request_ack(revision: int, operation_id: str) -> dict:
            """Acknowledge the received request revision before mutations."""
            return mutate("ack", revision, operation_id)

        @server.tool()
        def hive_task_create(
            description: str,
            revision: int,
            operation_id: str,
            dependencies: list[str] | None = None,
        ) -> dict:
            """Create complementary work; dependencies must already exist in this revision."""
            return mutate(
                "task_create",
                revision,
                operation_id,
                description=description,
                dependencies=dependencies or [],
            )

        @server.tool()
        def hive_task_claim(revision: int, operation_id: str, task_id: str | None = None) -> dict:
            """Atomically own one available task. A null task_id means no work is ready."""
            return mutate("task_claim", revision, operation_id, task_id=task_id)

        @server.tool()
        def hive_task_release(
            task_id: str, claim_version: int, revision: int, operation_id: str
        ) -> dict:
            """Release only your current claim."""
            return mutate(
                "task_release", revision, operation_id, task_id=task_id, claim_version=claim_version
            )

        @server.tool()
        def hive_task_complete(
            task_id: str, claim_version: int, publication_id: str, revision: int, operation_id: str
        ) -> dict:
            """Complete a current claim with your immutable current-revision publication."""
            return mutate(
                "task_complete",
                revision,
                operation_id,
                task_id=task_id,
                claim_version=claim_version,
                publication_id=publication_id,
            )

        @server.tool()
        def hive_context_publish(
            kind: str,
            message: str,
            paths: list[str],
            revision: int,
            operation_id: str,
            supersedes: str | None = None,
        ) -> dict:
            """Freeze selected files with a fact/fail/correction/completion publication."""
            return mutate(
                "publish",
                revision,
                operation_id,
                kind=kind,
                message=message,
                paths=paths,
                supersedes=supersedes,
            )

        @server.tool()
        def hive_artifact_expand(publication_id: str, paths: list[str] | None = None) -> dict:
            """Read publication metadata and optional selected file contents, at most 64 KiB."""
            return client.call("expand", {"publication_id": publication_id, "paths": paths or []})

        @server.tool()
        def hive_artifact_apply(publication_id: str, revision: int, operation_id: str) -> dict:
            """Apply files in your workspace only if their baseline is compatible."""
            return mutate("import", revision, operation_id, publication_id=publication_id)

        @server.tool()
        def hive_worker_yield(revision: int, operation_id: str) -> dict:
            """No useful independent work remains: register wait, then end your turn."""
            return mutate("yield", revision, operation_id)

        @server.tool()
        def hive_integration_claim(revision: int, operation_id: str) -> dict:
            """Temporarily own assembling the single result; no permanent manager."""
            return mutate("integration_claim", revision, operation_id)

        @server.tool()
        def hive_candidate_submit(
            summary: str, artifacts: list[str], revision: int, operation_id: str
        ) -> dict:
            """Account for requirements and freeze your result. Explicit artifacts may be []."""
            return mutate("candidate", revision, operation_id, summary=summary, artifacts=artifacts)
    else:

        @server.tool()
        def hive_run_start(
            project: str,
            request: str,
            operation_id: str,
            model: str | None = None,
            force: bool = False,
            experimental_cursor: bool = False,
            check_argv: list[str] | None = None,
        ) -> dict:
            """Start real Cursor workers. Experimental adapter requires explicit opt-in.

            The request is MCP-supplied text, not a native clone of the current chat.
            """
            return start_run(
                project,
                request,
                home=home,
                operation_id=operation_id,
                model=model,
                force=force,
                experimental_cursor=experimental_cursor,
                check_argv=check_argv,
            )

        @server.tool()
        def hive_run_status(run_id: str) -> dict:
            """Read retained/live run state. Exported does not mean original project ready."""
            return status(home, run_id)

        @server.tool()
        def hive_run_update(
            run_id: str, request: str, expected_revision: int, operation_id: str
        ) -> dict:
            """Replace the full request; pending acknowledgement is not delivered acknowledgement."""
            return control(
                home,
                run_id,
                "update",
                {"request": request, "expected_revision": expected_revision},
                operation_id,
            )

        @server.tool()
        def hive_run_stop(run_id: str, operation_id: str) -> dict:
            """Request cancellation and preserve work. Poll status for confirmed stop."""
            return control(home, run_id, "stop", operation_id=operation_id)

        @server.tool()
        def hive_recover_export(run_id: str, destination: str, worker: str | None = None) -> dict:
            """Export immutable candidate or stopped retained worker into a new directory."""
            return recover(home, run_id, destination, worker)

    return server


def serve(home=None):
    make_server(home).run(transport="stdio")
