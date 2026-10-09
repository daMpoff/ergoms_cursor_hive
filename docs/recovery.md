# Recovery and retained work

0.1 never applies an export automatically. Inspect report.json and export/manifest.json. files/ contains changed results; bases/ contains admitted original bytes; deleted lists proposed deletions. Existing original files and their Git index remain intact.

Recover a selected immutable candidate:

```bash
cursor-hive recover RUN_UUID /new/outside-source/directory
```

Without a selected candidate, stop the supervisor and explicitly choose a retained worker:

```bash
cursor-hive recover RUN_UUID /new/outside-source/directory --worker a
```

Do not remove workers while process ownership is unresolved. Recovery refuses a mutable worker with a live supervisor or known retained writers. Unknown saved process identity is treated conservatively. There is no stale-lease takeover or PID-only automatic kill.

Repeated export to an intact matching bundle is idempotent and verifies result/base hashes. A foreign existing directory is never overwritten. An interrupted publishing directory carries incomplete.json and is not complete; choose a fresh destination to recover.

Project locks are released only after confirmed shutdown of recorded writers. If the inspector cannot establish detached descendant coverage, retain the work and qualify the host; this prototype does not claim hostile process isolation.
