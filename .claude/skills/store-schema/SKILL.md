---
name: store-schema
description: Evolve the SQLite config store — schema, queries, and encrypted secrets. Use when adding/altering a stored entity (benches, suites, users, inventory, secrets…).
---

# Evolve the config store

The store is SQLite and is the **source of truth** for configuration (benches, suites, users,
board-models, inventory, secrets). YAML only seeds/imports/exports it.

```
atf/store/
  schema.sql   table definitions (created on first `atf web`)
  db.py        connection + path (DATABASE_URL or reports/atf.db) + WAL
  crypto.py    Fernet at-rest encryption, keyed by APP_SECRET
  repo.py      ALL queries live here (one class); web/cli call these, never raw SQL
```

## Steps for a schema change

1. **`schema.sql`** — add/alter the table. Keep it idempotent (`CREATE TABLE IF NOT EXISTS`,
   additive columns). There is no migration engine; the schema is applied on connect, so favour
   additive changes and tolerate older rows.
2. **`repo.py`** — add the query methods (list/get/upsert/delete). Match the existing style: take
   the lock, use parameterized SQL, return plain dicts/dataclasses. Never build SQL by string
   interpolation of user input.
3. **Secrets** — any secret value goes through `crypto.py` (encrypt on write, decrypt on read).
   Never store or return plaintext, never log it. Encryption is keyed by `APP_SECRET`; a change to
   the value of a *hash salt* or `APP_SECRET` invalidates existing data, so treat those as fixed.
4. **Seed/import** — if the entity is seedable from YAML, wire it into the first-run seed and the
   `atf store import/export` paths so config stays reproducible.
5. **API** — expose via `repo` in `web/server.py` (see the `web-api` skill), gating admin-only
   operations with `require_admin`.

## Verify

```bash
rm -f reports/atf.db*      # optional: start from a fresh store in dev
make check
.venv/bin/atf web          # first run creates the schema; exercise the new CRUD via /api/docs
```
