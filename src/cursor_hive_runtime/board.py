"""Transactional board. Only authenticated runtime dispatchers assign actor identity."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import HiveError
from .util import canonical, digest


class Board:
    def __init__(self, path: Path):
        self.path = path
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks(
                    id TEXT PRIMARY KEY, description TEXT NOT NULL, state TEXT NOT NULL,
                    owner TEXT, claim_version INTEGER NOT NULL DEFAULT 0,
                    revision INTEGER NOT NULL, dependencies TEXT NOT NULL,
                    publication_id TEXT);
                CREATE UNIQUE INDEX IF NOT EXISTS one_claim ON tasks(owner)
                    WHERE state='claimed';
                CREATE TABLE IF NOT EXISTS publications(
                    id TEXT PRIMARY KEY, author TEXT NOT NULL, revision INTEGER NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL, manifest TEXT NOT NULL,
                    supersedes TEXT);
                CREATE TABLE IF NOT EXISTS events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS operations(
                    actor TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL,
                    response TEXT NOT NULL, PRIMARY KEY(actor,id));
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @staticmethod
    def get(db, key, default=None):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(db, key, value):
        db.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, canonical(value)))

    @staticmethod
    def event(db, kind, payload):
        db.execute(
            "INSERT INTO events(kind,payload,created) VALUES(?,?,?)",
            (kind, canonical(payload), time.time()),
        )

    def initialize(self, run_id, project, request):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.get(db, "run_id"):
                raise HiveError("already_initialized", "Board already belongs to a run")
            for k, v in {
                "run_id": run_id,
                "project": str(project),
                "request": request,
                "revision": 1,
                "state": "starting",
                "stop_requested": False,
            }.items():
                self.put(db, k, v)
            self.event(db, "run_created", {"run_id": run_id})
            db.commit()

    def set_meta(self, key, value):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            self.put(db, key, value)
            self.event(db, key, value)
            db.commit()

    def meta(self, key, default=None):
        with self.connection() as db:
            return self.get(db, key, default)

    def cached(self, actor, op, method, args):
        if not op or len(op) > 160:
            raise HiveError("invalid_operation", "A nonempty operation_id is required")
        with self.connection() as db:
            return self._cached(db, actor, op, method, args)

    @staticmethod
    def _cached(db, actor, op, method, args):
        key = digest(canonical([method, args]).encode())
        row = db.execute(
            "SELECT digest,response FROM operations WHERE actor=? AND id=?", (actor, op)
        ).fetchone()
        if row:
            if row[0] != key:
                raise HiveError("operation_conflict", "operation_id was used with other arguments")
            return json.loads(row[1])
        return None

    def mutate(self, actor, op, method, args, prepared=None):
        self.cached(actor, op, method, args)  # Validate even on direct internal callers.
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._cached(db, actor, op, method, args)
            if old is not None:
                db.rollback()
                return old
            revision = self.get(db, "revision")
            if actor in ("a", "b"):
                if self.get(db, "state") != "running":
                    raise HiveError("run_not_active", "Worker mutation outside running state")
                if args.get("revision") != revision:
                    raise HiveError("stale_revision", "Worker request revision is stale")
                if method != "ack" and self.get(db, f"ack:{actor}") != revision:
                    raise HiveError("ack_required", "Acknowledge current request before mutations")
            fn = getattr(self, f"_do_{method}", None)
            if not fn:
                raise HiveError("unknown_method", method)
            response = fn(db, actor, args, prepared)
            db.execute(
                "INSERT INTO operations VALUES(?,?,?,?)",
                (actor, op, digest(canonical([method, args]).encode()), canonical(response)),
            )
            self.event(db, method, {"actor": actor, "result": response})
            db.commit()
            return response

    def _do_task_create(self, db, actor, args, _):
        description = args.get("description", "")
        deps = args.get("dependencies", [])
        if not isinstance(description, str) or not 1 <= len(description) <= 12000:
            raise HiveError("invalid_task", "Task description is empty or too large")
        if not isinstance(deps, list) or len(deps) > 32:
            raise HiveError("invalid_dependency", "Invalid dependency list")
        if (
            db.execute(
                "SELECT COUNT(*) FROM tasks WHERE revision=?", (args["revision"],)
            ).fetchone()[0]
            >= 100
        ):
            raise HiveError("task_limit", "MVP allows at most 100 tasks per revision")
        for dep in deps:
            if not db.execute(
                "SELECT id FROM tasks WHERE id=? AND revision=?", (dep, args["revision"])
            ).fetchone():
                raise HiveError("invalid_dependency", "Dependency not in current revision")
        task_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO tasks(id,description,state,revision,dependencies) VALUES(?,?,?,?,?)",
            (task_id, description, "available", args["revision"], canonical(deps)),
        )
        return {"task_id": task_id}

    def _do_task_claim(self, db, actor, args, _):
        if db.execute(
            "SELECT id FROM tasks WHERE state='claimed' AND owner=?", (actor,)
        ).fetchone():
            raise HiveError("already_claimed", "Worker already owns a task")
        rows = db.execute(
            "SELECT * FROM tasks WHERE state='available' AND revision=? ORDER BY rowid",
            (args["revision"],),
        ).fetchall()
        for row in rows:
            if args.get("task_id") and row["id"] != args["task_id"]:
                continue
            deps = json.loads(row["dependencies"])
            if any(
                db.execute("SELECT state FROM tasks WHERE id=?", (d,)).fetchone()[0] != "completed"
                for d in deps
            ):
                continue
            db.execute(
                "UPDATE tasks SET state='claimed',owner=?,claim_version=claim_version+1 "
                "WHERE id=? AND state='available'",
                (actor, row["id"]),
            )
            return {
                "task_id": row["id"],
                "description": row["description"],
                "claim_version": row["claim_version"] + 1,
            }
        return {"task_id": None}

    def owned_task(self, db, actor, args):
        row = db.execute("SELECT * FROM tasks WHERE id=?", (args.get("task_id"),)).fetchone()
        if (
            not row
            or row["state"] != "claimed"
            or row["owner"] != actor
            or row["claim_version"] != args.get("claim_version")
            or row["revision"] != args["revision"]
        ):
            raise HiveError("stale_claim", "Task ownership is not current")
        return row

    def _do_task_release(self, db, actor, args, _):
        self.owned_task(db, actor, args)
        db.execute(
            "UPDATE tasks SET state='available',owner=NULL,claim_version=claim_version+1 "
            "WHERE id=?",
            (args["task_id"],),
        )
        return {"released": True}

    def _do_task_complete(self, db, actor, args, _):
        self.owned_task(db, actor, args)
        pub = db.execute(
            "SELECT * FROM publications WHERE id=?", (args.get("publication_id"),)
        ).fetchone()
        if not pub or pub["author"] != actor or pub["revision"] != args["revision"]:
            raise HiveError("invalid_publication", "Completion needs your current publication")
        db.execute(
            "UPDATE tasks SET state='completed',publication_id=? WHERE id=?",
            (args["publication_id"], args["task_id"]),
        )
        return {"completed": True}

    def _do_publish(self, db, actor, args, prepared):
        kind, message = args.get("kind"), args.get("message")
        if (
            kind not in ("fact", "fail", "correction", "completion")
            or not isinstance(message, str)
            or not 1 <= len(message) <= 16000
        ):
            raise HiveError("invalid_publication", "Invalid kind or message")
        supersedes = args.get("supersedes")
        if (
            supersedes
            and not db.execute("SELECT id FROM publications WHERE id=?", (supersedes,)).fetchone()
        ):
            raise HiveError("invalid_publication", "Missing referenced publication")
        publication_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO publications VALUES(?,?,?,?,?,?,?)",
            (
                publication_id,
                actor,
                args["revision"],
                kind,
                message,
                canonical(prepared or {}),
                supersedes,
            ),
        )
        return {"publication_id": publication_id}

    def _do_ack(self, db, actor, args, _):
        self.put(db, f"ack:{actor}", args["revision"])
        return {"acknowledged_revision": args["revision"]}

    def _do_import(self, db, actor, args, prepared):
        return prepared

    def _do_yield(self, db, actor, args, _):
        seq = db.execute("SELECT COALESCE(MAX(sequence),0) FROM events").fetchone()[0]
        self.put(db, f"yield:{actor}", {"sequence": seq, "revision": args["revision"]})
        return {"end_turn": True, "sequence": seq}

    def _do_integration_claim(self, db, actor, args, _):
        owner = self.get(db, "integration_owner")
        if owner and owner != actor:
            raise HiveError("integration_busy", "Another worker integrates this result")
        self.put(db, "integration_owner", actor)
        return {"owner": actor}

    def _do_candidate(self, db, actor, args, prepared):
        if self.get(db, "integration_owner") != actor:
            raise HiveError("integration_required", "Claim integration first")
        if self.get(db, f"ack:{actor}") != args["revision"]:
            raise HiveError("ack_required", "Acknowledge request revision first")
        if any(self.get(db, f"ack:{peer}") != args["revision"] for peer in ("a", "b")):
            raise HiveError("peer_ack_pending", "Both peers must receive the current request")
        if db.execute(
            "SELECT id FROM tasks WHERE revision=? AND state NOT IN ('completed','cancelled')",
            (args["revision"],),
        ).fetchone():
            raise HiveError("unfinished_tasks", "Current tasks are unfinished")
        if not db.execute("SELECT id FROM tasks WHERE revision=?", (args["revision"],)).fetchone():
            raise HiveError("missing_tasks", "No work recorded for current request")
        unresolved = db.execute(
            "SELECT p.id FROM publications p WHERE p.kind='fail' AND p.revision=? "
            "AND NOT EXISTS(SELECT 1 FROM publications c "
            "WHERE c.kind='correction' AND c.supersedes=p.id)",
            (args["revision"],),
        ).fetchone()
        if unresolved:
            raise HiveError("unresolved_fail", "Current failure has no correction")
        if not isinstance(args.get("summary"), str) or not args["summary"].strip():
            raise HiveError("missing_summary", "Account for request requirements")
        if self.get(db, "candidate"):
            raise HiveError("candidate_exists", "A candidate is already selected")
        candidate = {
            "id": str(uuid.uuid4()),
            "worker": actor,
            "revision": args["revision"],
            "summary": args["summary"],
            "manifest": prepared,
        }
        self.put(db, "candidate", candidate)
        return {"candidate_id": candidate["id"]}

    def _do_update(self, db, actor, args, _):
        if self.get(db, "state") not in ("starting", "running"):
            raise HiveError("update_closed", "Run no longer accepts updates")
        if args.get("expected_revision") != self.get(db, "revision"):
            raise HiveError("stale_revision", "Controller revision is stale")
        text = args.get("request", "")
        if not isinstance(text, str) or not text.strip() or len(text) > 64000:
            raise HiveError("invalid_request", "Expected a complete replacement request")
        revision = self.get(db, "revision") + 1
        self.put(db, "request", text)
        self.put(db, "revision", revision)
        self.put(db, "candidate", None)
        self.put(db, "integration_owner", None)
        db.execute(
            "UPDATE tasks SET state='cancelled',owner=NULL,claim_version=claim_version+1 "
            "WHERE state!='completed'"
        )
        return {"revision": revision, "delivery": "pending"}

    def _do_stop(self, db, actor, args, _):
        self.put(db, "stop_requested", True)
        return {"stop_requested": True}

    def close_admission(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            candidate = self.get(db, "candidate")
            if (
                self.get(db, "stop_requested")
                or not candidate
                or candidate["revision"] != self.get(db, "revision")
            ):
                return None
            self.put(db, "state", "finishing")
            self.event(db, "finishing", {"candidate_id": candidate["id"]})
            db.commit()
            return candidate

    def publication(self, pub_id):
        with self.connection() as db:
            row = db.execute("SELECT * FROM publications WHERE id=?", (pub_id,)).fetchone()
            if not row:
                raise HiveError("not_found", "Publication not found")
            value = dict(row)
            value["manifest"] = json.loads(value["manifest"])
            return value

    def context(self, after=0, limit=50):
        if (
            not isinstance(after, int)
            or after < 0
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise HiveError("invalid_cursor", "Invalid cursor or page size")
        with self.connection() as db:
            events = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM events WHERE sequence>? ORDER BY sequence LIMIT ?",
                    (after, limit),
                )
            ]
            for event in events:
                event["payload"] = json.loads(event["payload"])
            tasks = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM tasks WHERE revision=? ORDER BY rowid LIMIT 100",
                    (self.get(db, "revision"),),
                )
            ]
            for task in tasks:
                task["dependencies"] = json.loads(task["dependencies"])
            total = db.execute(
                "SELECT COUNT(*) FROM tasks WHERE revision=?", (self.get(db, "revision"),)
            ).fetchone()[0]
            return {
                "run_id": self.get(db, "run_id"),
                "state": self.get(db, "state"),
                "revision": self.get(db, "revision"),
                "request": self.get(db, "request"),
                "tasks": tasks,
                "tasks_total": total,
                "tasks_truncated": total > len(tasks),
                "events": events,
                "cursor": events[-1]["sequence"] if events else after,
                "has_more": bool(
                    events
                    and db.execute(
                        "SELECT 1 FROM events WHERE sequence>?", (events[-1]["sequence"],)
                    ).fetchone()
                ),
                "candidate": self.get(db, "candidate"),
                "report": self.get(db, "report"),
            }
