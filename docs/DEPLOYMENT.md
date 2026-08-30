# Deployment

This document describes a local evaluation setup, not a production runbook.

## Requirements

- Python 3.11 or 3.12
- Node.js 22 and pnpm for the Browser Worker
- Chrome or Chromium when browser-backed verification is enabled
- Optional external Hermes Agent 0.19.0 for MCP integration review

## Local preparation

1. Create a Python virtual environment and install `requirements.txt`.
2. Copy `.env.example` to `.env` and keep real values local.
3. Create `.runtime/`; initialize a new SQLite database from `cde-data/schema/001_initial.sql` or `cde-data/scripts/init_db.py`.
4. Obtain public trial information according to the source site's terms. Do not commit the resulting database or archived pages.
5. For the Browser Worker, copy `config/worker.example.yaml` to an ignored local file and set `chrome_executable` to an absolute local path.

## Optional model routing

Model routing is disabled by default. If enabled, set provider configuration locally. Never commit API keys, custom provider URLs, request captures, or model responses that contain user data. Deterministic privacy and evidence checks remain mandatory.

## Optional Hermes integration

Hermes is not bundled. Review `THIRD_PARTY_NOTICES.md`, install Hermes independently, and configure Angela's MCP server through a local Hermes configuration that is excluded from Git. Version-specific patches under `hermes-cde-tool/patches` require manual review before use.

## Platform notes

- The data synchronization lock supports Unix `fcntl` and Windows `msvcrt`.
- Browser tests that require an installed browser may be skipped or limited on Windows; Ubuntu CI validates build and offline unit tests.
- Process signaling and background service behavior should be validated on the intended operating system.

Do not reuse production databases, browser profiles, Hermes homes, or service configuration for portfolio evaluation.
