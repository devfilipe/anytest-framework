"""atf config store: SQLite-backed benches / suites / secrets (encrypted) with YAML
import/export. `open_repo()` wires db + cipher + Repo together."""
from __future__ import annotations


def open_repo(db_path: str | None = None, app_secret: str | None = None):
    from atf.store.crypto import Cipher
    from atf.store.db import open_db
    from atf.store.repo import Repo
    return Repo(open_db(db_path), Cipher(app_secret))
