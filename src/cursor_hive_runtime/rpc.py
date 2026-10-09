import hmac
import http.client
import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import PROTOCOL_VERSION
from .errors import HiveError
from .util import canonical
from .workspace import (
    apply_publication,
    object_bytes,
    publication_snapshot,
    snapshot,
)

WORKER_METHODS = {
    "context",
    "expand",
    "ack",
    "task_create",
    "task_claim",
    "task_release",
    "task_complete",
    "publish",
    "import",
    "yield",
    "integration_claim",
    "candidate",
}
CONTROLLER_METHODS = {"context", "update", "stop"}


class Client:
    def __init__(self, url, token):
        self.url, self.token = url, token

    def call(self, method, args=None, operation_id=None):
        body = canonical(
            {
                "protocol_version": PROTOCOL_VERSION,
                "method": method,
                "args": args or {},
                "operation_id": operation_id,
            }
        ).encode()
        request = urllib.request.Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"},
        )
        # Never send localhost credentials via a user's configured HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=15) as response:
                result = json.load(response)
        except urllib.error.HTTPError as e:
            try:
                error = json.load(e)["error"]
                raise HiveError(error["code"], error["message"]) from e
            except (KeyError, json.JSONDecodeError):
                raise HiveError("rpc_error", "Invalid server error") from e
        except (OSError, http.client.HTTPException) as e:
            raise HiveError(
                "runtime_unreachable", "Runtime is not reachable; retained state is available"
            ) from e
        return result["result"]


class RuntimeAPI:
    def __init__(self, board, run, controller_token):
        self.board, self.run = board, run
        self.tokens = {controller_token: ("controller", None)}
        self.token_lock = threading.Lock()
        self.io_locks = {a: threading.Lock() for a in ("a", "b")}
        self.changed = threading.Event()

    def bind(self, actor, token):
        with self.io_locks[actor], self.token_lock:
            self.tokens = {
                t: principal for t, principal in self.tokens.items() if principal[0] != actor
            }
            if token:
                self.tokens[token] = (actor, self.board.meta("revision"))

    def actor(self, token):
        with self.token_lock:
            for saved, principal in self.tokens.items():
                if hmac.compare_digest(saved, token):
                    return principal
        raise HiveError("unauthorized", "Invalid or expired runtime binding")

    def dispatch(self, actor, method, args, op, binding_revision=None, authenticated_token=None):
        allowed = CONTROLLER_METHODS if actor == "controller" else WORKER_METHODS
        if method not in allowed:
            raise HiveError("forbidden", "Tool is not available to this principal")
        if (
            actor != "controller"
            and method not in ("context", "expand")
            and binding_revision is not None
            and args.get("revision") != binding_revision
        ):
            raise HiveError("stale_turn", "Binding belongs to an earlier request turn")
        if method == "context":
            value = self.board.context(args.get("after", 0), args.get("limit", 50))
            if actor != "controller" and value["candidate"]:
                value["candidate"] = {
                    k: v for k, v in value["candidate"].items() if k != "manifest"
                }
            return value
        if method == "expand":
            pub = self.board.publication(args.get("publication_id"))
            requested = args.get("paths", [])
            if not isinstance(requested, list) or len(requested) > 8:
                raise HiveError("invalid_paths", "Expand at most 8 selected files")
            pub["content"] = {}
            total = 0
            for name in requested:
                if name not in pub["manifest"]:
                    raise HiveError("not_found", "Path is not in this publication")
                data = object_bytes(self.run, pub["manifest"][name]["result"])
                total += len(data)
                if total > 65536:
                    raise HiveError("expand_limit", "Select smaller files; expand limit is 64 KiB")
                pub["content"][name] = data.decode("utf-8")
            return pub
        # Prevent replayed side effects and re-capture before checking the durable operation.
        cached = self.board.cached(actor, op, method, args)
        if cached is not None:
            return cached
        lock = self.io_locks.get(actor)
        if lock:
            with lock:
                if authenticated_token is not None:
                    self.actor(authenticated_token)
                value = self._mutation(actor, method, args, op)
        else:
            value = self._mutation(actor, method, args, op)
        self.changed.set()
        return value

    def _mutation(self, actor, method, args, op):
        cached = self.board.cached(actor, op, method, args)
        if cached is not None:
            return cached
        prepared = None
        if actor in ("a", "b"):
            if self.board.meta("state") != "running" or args.get("revision") != self.board.meta(
                "revision"
            ):
                raise HiveError("stale_revision", "Worker request is not active")
            if method != "ack" and self.board.meta(f"ack:{actor}") != args["revision"]:
                raise HiveError("ack_required", "Acknowledge current request first")
            workspace = self.run / "workers" / actor
            if method == "publish":
                prepared = publication_snapshot(self.run, workspace, args.get("paths", []))
            elif method == "import":
                prepared = apply_publication(
                    self.run,
                    workspace,
                    self.board.publication(args.get("publication_id")),
                    actor + ":" + op,
                )
            elif method == "candidate":
                artifacts = args.get("artifacts")
                if not isinstance(artifacts, list) or len(artifacts) > 64:
                    raise HiveError(
                        "artifact_contract_required", "Provide explicit artifacts, possibly []"
                    )
                from .workspace import git_names

                prepared = snapshot(workspace, self.run, git_names(workspace) + artifacts)["files"]
        return self.board.mutate(actor, op, method, args, prepared)


def start_server(api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            code, response = 200, None
            try:
                if self.path != "/rpc":
                    raise HiveError("not_found", "Unknown endpoint")
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                actor, binding_revision = api.actor(token)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 65536:
                    raise HiveError("request_limit", "RPC request exceeds 64 KiB")
                msg = json.loads(self.rfile.read(size))
                if msg.get("protocol_version") != PROTOCOL_VERSION:
                    raise HiveError("protocol_mismatch", "Incompatible protocol version")
                if not isinstance(msg.get("args", {}), dict):
                    raise HiveError("invalid_arguments", "Arguments must be an object")
                response = {
                    "result": api.dispatch(
                        actor,
                        msg.get("method"),
                        msg.get("args", {}),
                        msg.get("operation_id"),
                        binding_revision,
                        token,
                    )
                }
            except HiveError as e:
                code, response = 400, {"error": e.as_dict()}
            except (ValueError, TypeError, KeyError):
                code, response = (
                    400,
                    {"error": {"code": "invalid_request", "message": "Malformed RPC request"}},
                )
            except Exception:
                code, response = (
                    500,
                    {"error": {"code": "runtime_error", "message": "Runtime operation failed"}},
                )
            body = canonical(response).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/rpc"
