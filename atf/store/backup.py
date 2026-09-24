"""Config-store backup & restore — a consistent single-file snapshot for moving a server.

A backup is a plain SQLite file produced with the online backup API (safe while the server
runs, WAL included). It carries the WHOLE config store: benches, suites, inventory, users,
board-models, check-sources and requirements.

Secrets stay **encrypted with `APP_SECRET`** in the snapshot. The target server must run with
the *same* `APP_SECRET` to decrypt them — otherwise config restores fine but secret values are
unreadable. Back up your `APP_SECRET` alongside the file.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

# a store snapshot must have at least these tables to be accepted on restore
_REQUIRED = {"bench", "app_user", "check_source"}


def snapshot(con: sqlite3.Connection, dest: str | Path) -> Path:
    """Write a consistent copy of the live store to `dest` (online backup, no downtime)."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    out = sqlite3.connect(str(dest))
    try:
        con.backup(out)          # online, transactionally consistent; folds in WAL
    finally:
        out.close()
    return dest


def restore(con: sqlite3.Connection, src: str | Path) -> None:
    """Replace the live store's contents with the snapshot at `src`, in place (no file swap, so
    the server's open connection stays valid). Validates it looks like an Anytest store first,
    then re-applies migrations so an older snapshot is brought up to the current schema."""
    src = Path(src)
    if not src.is_file():
        raise FileNotFoundError(f"backup file not found: {src}")
    probe = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        try:
            tables = {r[0] for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        except sqlite3.DatabaseError as e:
            raise ValueError(f"not a valid SQLite backup file: {e}") from e
        missing = _REQUIRED - tables
        if missing:
            raise ValueError(f"not an Anytest config store (missing tables: {sorted(missing)})")
        probe.backup(con)        # overwrite every page of the live DB from the snapshot
    finally:
        probe.close()
    from atf.store.db import migrate
    migrate(con)                 # additive migrations for a snapshot from an older version
    con.commit()
