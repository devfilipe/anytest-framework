"""SQLite connection + schema migration. The single spot that knows the DB engine — swap
this (and the schema dialect) for Postgres later without touching the Repository."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

_SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def db_path(default: str = "reports/atf.db") -> str:
    """`DATABASE_URL=file:/path` (URL-style) or a bare path; else the default."""
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("file:"):
        return url[len("file:"):]
    return url or default


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or db_path()
    Path(p).parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: FastAPI runs sync routes in a threadpool. Low concurrency
    # here; SQLite serialises writes itself. A Postgres swap would drop this entirely.
    con = sqlite3.connect(p, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def migrate(con: sqlite3.Connection) -> None:
    con.executescript(_SCHEMA.read_text())
    _add_columns(con, "board_model", {"slug": "TEXT NOT NULL DEFAULT ''"})   # additive migrations
    _add_columns(con, "app_user", {"agent_token": "TEXT NOT NULL DEFAULT ''"})
    _add_columns(con, "report", {"select_json": "TEXT NOT NULL DEFAULT '{}'",
                                 "meta_json": "TEXT NOT NULL DEFAULT '{}'"})
    _add_columns(con, "inv_agent", {"last_editor": "TEXT NOT NULL DEFAULT ''",
                                    "updated_at": "TEXT NOT NULL DEFAULT ''",
                                    "comments": "TEXT NOT NULL DEFAULT ''",
                                    "ssh_port": "INTEGER NOT NULL DEFAULT 22"})
    _add_columns(con, "inv_board", {"last_editor": "TEXT NOT NULL DEFAULT ''",
                                    "updated_at": "TEXT NOT NULL DEFAULT ''",
                                    "comments": "TEXT NOT NULL DEFAULT ''"})
    _add_columns(con, "check_source", {"token_enc": "TEXT NOT NULL DEFAULT ''",
                                       "kind": "TEXT NOT NULL DEFAULT 'git'",
                                       "last_commit": "TEXT", "last_sync_by": "TEXT"})
    # drivers/actions became inventory entities: alias→entity link columns on the per-bench wiring
    _add_columns(con, "bench_vector", {"driver_name": "TEXT NOT NULL DEFAULT ''"})
    _add_columns(con, "bench_hook", {"action_name": "TEXT NOT NULL DEFAULT ''"})
    _add_columns(con, "inv_driver", {"description": "TEXT NOT NULL DEFAULT ''"})
    _add_columns(con, "inv_action", {"description": "TEXT NOT NULL DEFAULT ''"})
    # built-in DRIVER TYPES (name + description + prop schema of {name, description}); a bench
    # instantiates one on a board with an alias (ctx key) + values. serial + ip carry the channels.
    import json as _json
    _serial = _json.dumps([
        {"name": "agent", "description": "The bench node (an agent) that bridges the serial console."},
        {"name": "transport", "description": "ssh (serial over the agent) or ser2net (raw TCP socket)."},
        {"name": "device", "description": "Serial device on the agent, e.g. /dev/ttyUSB0."},
        {"name": "baud", "description": "Serial baud rate, e.g. 115200."},
    ])
    _ip = _json.dumps([
        {"name": "agent", "description": "Optional node to probe from (its L2 vantage). Blank = probe from the host/container (nmap)."},
        {"name": "ip", "description": "The target IP address (ctx.<alias>.ip)."},
    ])
    con.execute("INSERT OR IGNORE INTO inv_driver(name, description, props_json) VALUES('serial', ?, ?)",
                ("Board serial console via an agent (send/expect/login).", _serial))
    con.execute("INSERT OR IGNORE INTO inv_driver(name, description, props_json) VALUES('ip', ?, ?)",
                ("Network target reached by IP (scan/tls/ping); agent = from that node, none = host/container.", _ip))
    _pc = _json.dumps([
        {"name": "off", "description": "Command that powers the board OFF (runs on the agent)."},
        {"name": "on", "description": "Command that powers the board ON."},
        {"name": "status", "description": "Command that reports the power state."},
    ])
    con.execute("INSERT OR IGNORE INTO inv_action(name, description, signals_json) VALUES('power-cycle', ?, ?)",
                ("Cold-boot the board via a controllable power source (e.g. a smart plug).", _pc))
    con.commit()


def _add_columns(con: sqlite3.Connection, table: str, cols: dict[str, str]) -> None:
    """Idempotently ALTER TABLE … ADD COLUMN for schema evolution on existing DBs."""
    have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def open_db(path: str | None = None) -> sqlite3.Connection:
    con = connect(path)
    migrate(con)
    return con
