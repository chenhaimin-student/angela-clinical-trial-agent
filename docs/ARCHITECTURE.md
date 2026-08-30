# Architecture

Angela separates deterministic privacy and evidence controls from optional model-assisted routing.

```text
de-identified request
        |
        v
privacy gate -> intent/fact normalization -> request scheduler/state
        |                                      |
        |                                      v
        |                         local evidence-bound query
        |                                      |
        |                         +------------+------------+
        |                         |                         |
        v                         v                         v
optional LLM router      SQLite data layer         Browser Worker
        |                 (read-first path)       (controlled live check)
        +-------------------------+-------------------------+
                                  |
                                  v
                     evidence filter and renderer
                                  |
                                  v
                   candidate information, not a verdict
```

## Components

- `angela_orchestration` owns privacy rejection, routing, structured patient facts, entity resolution, evidence review, concurrency leases, traces, and request state.
- `cde_store` owns the SQLite schema, public-page parsing, synchronization policy, and deterministic local queries.
- `cde_demo` provides a small local client and report rendering for fictional inputs.
- `cde-browser-worker` isolates browser automation behind a loopback-only HTTP service and strict configuration schema.
- `hermes-cde-tool` exposes Angela's MCP tools and integration patches while keeping Hermes as an external dependency.
- `session-compactor` coordinates optional compaction work outside the foreground request path and aborts when a new message changes the transcript.
- `quality_eval` checks contracts and evidence boundaries with offline fixtures.

## Trust boundaries

1. Direct identifiers are rejected before routing.
2. The local database is deployment-owned and absent from Git.
3. Browser access is optional, loopback-only, and must use a dedicated local profile outside Git.
4. Model output cannot override deterministic privacy blocks or unsupported evidence.
5. Hermes credentials, state, and message history remain outside the repository.

## Data ownership

The repository contains schemas, parsers, fictional examples, and tests. It contains no production database, raw archived page, patient record, chat transcript, browser profile, or credential.
