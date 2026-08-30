# Testing

Run one group at a time so failures are attributable and resource use remains predictable.

## Python groups

```bash
python -m unittest discover -s hermes-cde-tool/tests -p "test_*.py"
python -m unittest discover -s cde-data/tests -p "test_*.py"
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s services/session-compactor/tests -p "test_*.py"
python -m unittest tests.test_public_templates
```

Set `PYTHONPATH` to include the repository root, `cde-data`, `cde-demo`, and `services/session-compactor` if the shell does not resolve local modules automatically.

## Browser Worker

```bash
cd cde-demo/cde-browser-worker
pnpm install --frozen-lockfile
pnpm build
pnpm test
```

Offline tests must not download real trial pages or use a production browser profile. Live smoke scripts are opt-in developer tools and are not executed by CI.

## Result classification

- **Passed:** completed with expected assertions.
- **Windows platform limitation:** an operating-system facility is unavailable and the code path is intentionally validated in Ubuntu CI.
- **Needs GitHub Ubuntu CI:** local dependencies or OS behavior cannot be reproduced safely on Windows.
- **Real failure:** assertion, build, schema, or security validation failed independent of platform.

Never relabel a real failure as a platform limitation.
