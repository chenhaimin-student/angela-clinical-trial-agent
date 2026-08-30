# Angela Hermes MCP Integration

This directory contains Angela-authored MCP tools and compatibility patches for an external Hermes Agent installation.

The MCP server exposes public-trial search, local trial details, questionnaire evidence, and controlled live verification. It expects a deployment-owned SQLite path and does not bundle data or credentials.

Hermes Agent 0.19.0 was the production-compatible reference version during portfolio preparation. Hermes source, its virtual environment, private home, model credentials, platform secrets, and runtime state are not included. See `THIRD_PARTY_NOTICES.md`.

Run the offline tests from the repository root:

```bash
python -m unittest discover -s hermes-cde-tool/tests -p "test_*.py"
```

Patch installers must be reviewed against the exact Hermes version before use. They are examples of Angela's integration work, not automatic deployment instructions.
