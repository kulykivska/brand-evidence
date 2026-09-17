# brand-evidence — rules for AI agents

This is a record-keeping tool. The evidence log is the point of the system.
Read `arch-rules.yaml`; `tests/test_arch_rules.py` enforces it. These rules override the
requirements document where they conflict.

## Never
- Add an LLM call anywhere in `core/`, `capture/`, `ingest/`, `sources/` or `export.py`.
  Classification lives only in `digest/classification.py`, behind `BE_ENABLE_CLASSIFICATION`,
  runs after the evidence write, and writes only to `mentions.notes`.
- Update or delete `evidence_log` rows, or change `compute_entry_hash`. A schema change that
  alters payload shape is fine; rewriting existing hashes is not.
- Store cookies, session state or secrets in the repo or the database.
- Generate legal, tax or compliance advice, or add speculative text to the digest. The digest
  reports facts and status. "0 new mentions." is a complete, correct section.
- Mirror full third-party article text. URL, title and a 2000-char excerpt only.
- Implement anything from requirements §2 "out of scope" or §14 "deferred".
- Write non-English text in code, comments, output or docs.

## Always
- Write a `runs` row before doing any work (`core/runs.tracked_run`).
- Record every write to posts / mentions / artifacts / archive_snapshots via
  `core/evidence_log.record` or `.append` in the same transaction.
- Prefer integrity over convenience when the two conflict.
- Keep code comments to two lines or fewer.
- Run `uv run pytest` before committing; the suite must pass with networking disabled.

## Layout
`src/brand_evidence/` — `config/` settings + sources.yaml, `core/` models, evidence log, store,
`capture/` Playwright + archive provider, `ingest/` hook (push), crawler (pull), archive poll,
backfill, `sources/` one module per source, `digest/` builder + template, `cli.py`, `app.py`.
Migrations in `alembic/`; the schema lives there, `create_all` is for tests only.
