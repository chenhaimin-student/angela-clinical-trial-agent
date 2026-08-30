# Privacy

Angela is designed for de-identified search criteria and public clinical-trial information.

## Allowed inputs

- disease or condition terms;
- broad age or birth-year information when needed for public criteria search;
- sex, city/region, trial phase, recruitment status, sponsor, public project code, and other non-identifying search constraints;
- clearly fictional examples used in tests.

## Prohibited inputs and repository content

- names, government identifiers, phone numbers, medical-record numbers, specimen identifiers, or contact details tied to a person;
- patient records, chat history, message replay, request state, cookies, tokens, credentials, browser profiles, logs, screenshots, or production diagnostics;
- production SQLite databases, backups, WAL/SHM files, and raw archived HTML;
- private messaging-platform IDs or configuration.

## Output boundary

The system may summarize public trial information and highlight criteria requiring professional review. It must not claim that a person is eligible, enrolled, diagnosed, or medically advised.

## Development practices

Use temporary databases and synthetic fixtures. Keep `.runtime/`, `.env`, databases, raw pages, reports, and browser state outside Git. Run secret and file-type scans before publication.
