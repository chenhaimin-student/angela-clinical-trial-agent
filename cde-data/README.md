# CDE Data Layer

This module stores and queries structured information derived from public clinical-trial pages.

## Included

- SQLite schema and compatible migrations;
- public-page parsers;
- catalog, daily, and policy-based synchronization services;
- deterministic local query contracts;
- fictional and temporary-database tests.

## Not included

No production database, backup, WAL/SHM file, raw page archive, synchronization report, browser profile, or source-site credential is included.

Initialize an empty local database under the ignored `.runtime/` directory:

```bash
python cde-data/scripts/init_db.py --database .runtime/cde_trials.sqlite3
```

Use `PYTHONPATH=cde-data` when running the data-layer tests if the module is not installed as a package.

Collection code must respect the public source's terms, rate limits, and access controls. The local query layer is the default path; browser-backed checks are optional and controlled separately.
