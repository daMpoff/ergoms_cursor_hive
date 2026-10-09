"""Deterministic demonstration; never executes the user's coding request."""

import os
import uuid
from pathlib import Path

from .rpc import Client


def main():
    actor = os.environ["CURSOR_HIVE_WORKER"]
    client = Client(os.environ["CURSOR_HIVE_URL"], os.environ["CURSOR_HIVE_TOKEN"])
    revision = client.call("context")["revision"]

    def mutate(method, **args):
        return client.call(method, {"revision": revision, **args}, str(uuid.uuid4()))

    mutate("ack")
    if actor == "a":
        first = mutate("task_create", description="Demo: publish file A")
        mutate(
            "task_create",
            description="Demo: reuse A and publish file B",
            dependencies=[first["task_id"]],
        )
        claim = mutate("task_claim", task_id=first["task_id"])
        Path("hive-demo-a.txt").write_text("Published by fake agent A\n", encoding="utf-8")
        pub = mutate(
            "publish", kind="fact", message="Demo file A is available", paths=["hive-demo-a.txt"]
        )
        mutate("task_complete", **{k: claim[k] for k in ("task_id", "claim_version")}, **pub)
        mutate("yield")
    else:
        context = client.call("context")
        first_task = next(t for t in context["tasks"] if t["state"] == "completed")
        mutate("import", publication_id=first_task["publication_id"])
        claim = mutate("task_claim")
        if not claim["task_id"]:
            raise RuntimeError("Demo B started without available work")
        Path("hive-demo-b.txt").write_text(
            Path("hive-demo-a.txt").read_text(encoding="utf-8") + "Reused by fake agent B\n",
            encoding="utf-8",
        )
        pub = mutate(
            "publish",
            kind="completion",
            message="Demo B reused A",
            paths=["hive-demo-a.txt", "hive-demo-b.txt"],
        )
        mutate("task_complete", **{k: claim[k] for k in ("task_id", "claim_version")}, **pub)
        mutate("integration_claim")
        mutate("candidate", summary="Deterministic transport demo, not the user task", artifacts=[])


if __name__ == "__main__":
    main()
