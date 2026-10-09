# Local protocol v1

The supervisor exposes POST /rpc on a random 127.0.0.1 port. No proxy is used for local requests. The private endpoint file contains the URL and controller token; worker tokens are passed via the child environment and rotated on turns.

Request fields: protocol_version=1, method, args, operation_id. Mutations require a nonempty stable operation ID. Reusing it with other arguments is an operation_conflict. Worker role, request revision and turn binding are server assigned. A worker token from an earlier revision cannot acknowledge the later one.

Read methods: context(after,limit), expand(publication_id,paths). Worker mutations: ack, task_create, task_claim, task_release, task_complete, publish, import, yield, integration_claim, candidate. Controller mutations: update(expected_revision,request), stop. Run creation/recovery are controller operations outside the active per-run endpoint.

Mutations and their events are committed in one SQLite transaction. File snapshots are captured before the transaction; immutable bytes are content addressed. Imports have a preparation journal; replay can safely accept already applied publication bytes, while incompatible local changes fail.

context has a monotonic event cursor, has_more and task truncation indicators. Version 0.1 caps tasks at 100 per request revision. Publication metadata appears in the event history, content is expanded separately. Failed/timed-out CLI output is not a terminal success.

candidate requires the integrating worker, current acknowledgement, completed current tasks, no unresolved current fail and an explicit artifacts list. The semantic summary is a worker account of requirements; runtime cannot prove its completeness by text alone. Runtime checks are observed separately.

update replaces the complete request. It increments revision, cancels unfinished prior tasks and invalidates a candidate/integration owner. Supervisor stops old turns before continuing existing session IDs. On finishing, update admission closes transactionally. stop remains available during checks.
