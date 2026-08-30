# Idle Session Compactor

The Session Compactor moves optional long-context compaction outside the foreground request path. A short-lived activity lease and transcript fingerprint allow a worker to stop when a new message arrives.

## Safety invariants

- one candidate session is processed at a time;
- a new message or active lease preempts the worker;
- rows are soft-archived, not deleted;
- a result is committed only when it is smaller than the active context;
- coordination records contain identifiers, counts, timestamps, and status, not message text;
- worker errors are reduced to status classes instead of copying raw stderr into audit records.

## Files

- `daemon.py`: scheduler and preemption loop;
- `worker.py`: isolated compaction attempt;
- `session_compactor_store.py`: coordination, cooldown, checkpoint, and audit metadata;
- `gateway_activity_lease.py`: optional gateway-side heartbeat adapter;
- `gateway_idle_compactor.patch` and installer: version-specific Hermes integration reference;
- `tests/`: temporary-database unit tests.

Runtime databases and logs belong in an external Hermes home or another ignored local directory. No real chat database or copied production-state integration test is included in the public repository.
