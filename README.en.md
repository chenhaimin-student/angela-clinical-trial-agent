# Angela Clinical Trial Agent

[中文](README.md)

Angela is a portfolio-oriented clinical-trial information retrieval and orchestration project. It separates public-data ingestion, structured search, evidence constraints, request orchestration, a browser worker, and Hermes MCP integration into testable modules.

> This project processes public clinical-trial information for retrieval and research-support demonstrations only. It is not a medical device and does not provide diagnosis, treatment advice, or final enrollment decisions. Trial participation must be determined by the responsible study organization and qualified professionals.

## Main components

- `angela_orchestration/`: privacy gates, intent routing, structured facts, evidence filtering, concurrency scheduling, and request state.
- `cde-data/`: parsers, a SQLite schema, synchronization services, and read-only queries for public CDE information. No production database or archived pages are included.
- `cde-demo/`: a local demo client and an isolated TypeScript Browser Worker.
- `hermes-cde-tool/`: Angela-authored MCP server, adapters, and version-specific Hermes patches. Hermes itself is not vendored.
- `services/session-compactor/`: preemptible long-session compaction coordination that should retain metadata, not message content.
- `quality_eval/`: offline contract and evidence-boundary evaluation utilities.

## Safety boundaries

- Inputs must be de-identified search criteria. Direct identifiers such as names, identity numbers, phone numbers, and medical-record numbers are rejected.
- The repository contains no production SQLite files, backups, logs, browser profiles, chat history, patient data, or credentials.
- Optional model routing runs behind deterministic privacy rules and evidence validation.
- The Browser Worker binds to `127.0.0.1` and requires a user-created local configuration.
- Results are candidate information with evidence summaries, never a final eligibility conclusion.

## Setup and tests

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements.txt
```

Run the Python groups separately:

```bash
python -m unittest discover -s hermes-cde-tool/tests -p "test_*.py"
python -m unittest discover -s cde-data/tests -p "test_*.py"
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s services/session-compactor/tests -p "test_*.py"
```

For the Browser Worker, run `pnpm install --frozen-lockfile`, `pnpm build`, and `pnpm test` in `cde-demo/cde-browser-worker`.

See [Architecture](docs/ARCHITECTURE.md), [Deployment](docs/DEPLOYMENT.md), [Testing](docs/TESTING.md), and [Privacy](docs/PRIVACY.md) for details.

## Licensing

Angela-authored code is available under the non-OSI [Portfolio Source-Available License 1.0](LICENSE). Hermes Agent 0.19.0 is third-party MIT-licensed software; this repository contains only Angela-authored integration code and patches. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
