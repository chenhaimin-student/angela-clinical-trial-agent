# Hermes Compatibility Patches

These Angela-authored files document or install narrow integration changes against Hermes interfaces used by the MCP server and optional Session Compactor.

- Review every target path and expected source marker before applying a patch.
- Apply only to a separately installed, compatible Hermes version.
- Never point an installer at a production environment without an independent review and backup process.
- Do not commit patched third-party source, generated backups, Hermes configuration, credentials, or runtime state to this repository.

The public repository keeps patch logic for portfolio review; it does not vendor the patched Hermes source tree.
