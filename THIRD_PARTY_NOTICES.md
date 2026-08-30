# Third-Party Notices

## Hermes Agent

- Project: Hermes Agent
- Production-compatible version reviewed for this portfolio: 0.19.0
- License declared by the installed distribution: MIT
- Relationship: third-party agent runtime

The public repository does not vendor the Hermes source tree, virtual environment, private home directory, runtime state, or configuration. Files under `hermes-cde-tool/` and Hermes-related files under `services/session-compactor/` are Angela-authored MCP integration, adapters, patch installers, patch descriptions, and tests. They may depend on Hermes 0.19.0 interfaces and must be reviewed again before use with another version.

Hermes names and upstream code remain the property of their respective owners. The Portfolio Source-Available License applies only to Angela-authored material and does not replace the Hermes MIT license.

Python and JavaScript dependencies are declared in requirement files, `package.json`, and `pnpm-lock.yaml`. Each dependency remains under its own license. Chrome or Chromium is an external runtime and is not distributed by this repository.
