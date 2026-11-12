---
name: schema-change
description: How to add a table or a column to the SQLite database additively. Use when a change needs new stored data.
---

The whole schema lives in `src/orchestrator/store.py`. Changes are additive only: older
databases must open and older code must keep working on a database the new code has used.

1. **A new table**: add `CREATE TABLE IF NOT EXISTS …` (and its indexes with
   `CREATE INDEX IF NOT EXISTS`) to `SCHEMA`. `Store.__init__` runs `SCHEMA` on every open.
2. **A new column on an existing table**: do not edit the table's `CREATE TABLE` alone.
   Add `(name, ddl)` to the table's list of added columns (`RUN_META_ADDED`,
   `RUN_USAGE_ADDED`), or a list like them, so that `Store` adds it with
   `ALTER TABLE … ADD COLUMN` when it is missing. Several processes may open the same
   database at once: keep the `duplicate column` tolerance. A new column needs a default or
   must allow `NULL`.
3. Never drop or rename a table or column, and never change a column's meaning.
4. Tests in `tests/test_store.py`: an old database gets the new table or column
   (`test_a_master_era_database_gets_every_new_table_and_column`, with
   `tests/fixtures/master_era_schema.sql`). Opening it twice changes nothing, and rows with
   columns from a newer version still load.
5. Queries go into the module that owns the table: `store.py`, or a feature module such as
   `board.py` or `decisions.py`. The interfaces reach them only through the facade
   (`service.py`); `tests/test_architecture.py` checks this.
6. Run the `check` skill.
