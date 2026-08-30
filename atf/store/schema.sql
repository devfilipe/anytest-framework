-- atf config store (SQLite). Normalised where it helps listing/editing; JSON for the
-- genuinely variable bits (vector bindings, hook actions). Secrets are encrypted at rest.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS bench (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS agent (
  id             INTEGER PRIMARY KEY,
  bench_id       INTEGER NOT NULL REFERENCES bench(id) ON DELETE CASCADE,
  name           TEXT NOT NULL,
  platform       TEXT NOT NULL DEFAULT 'linux',
  host           TEXT NOT NULL DEFAULT '',
  ssh_user       TEXT NOT NULL DEFAULT '',
  ssh_secret_ref TEXT NOT NULL DEFAULT '',
  UNIQUE(bench_id, name)
);

CREATE TABLE IF NOT EXISTS board (
  id          INTEGER PRIMARY KEY,
  bench_id    INTEGER NOT NULL REFERENCES bench(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  model       TEXT NOT NULL DEFAULT '',
  serial      TEXT NOT NULL DEFAULT '',
  mgmt_ip      TEXT,
  mgmt_prefix  INTEGER,
  mgmt_gateway TEXT,
  UNIQUE(bench_id, name)
);

CREATE TABLE IF NOT EXISTS board_cred (
  id         INTEGER PRIMARY KEY,
  board_id   INTEGER NOT NULL REFERENCES board(id) ON DELETE CASCADE,
  role       TEXT NOT NULL,
  username   TEXT NOT NULL DEFAULT '',
  secret_ref TEXT NOT NULL DEFAULT '',
  UNIQUE(board_id, role)
);

CREATE TABLE IF NOT EXISTS board_vector (
  id          INTEGER PRIMARY KEY,
  board_id    INTEGER NOT NULL REFERENCES board(id) ON DELETE CASCADE,
  vector      TEXT NOT NULL,                 -- console | craft | mgmt
  config_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(board_id, vector)
);

CREATE TABLE IF NOT EXISTS board_hook (
  id           INTEGER PRIMARY KEY,
  board_id     INTEGER NOT NULL REFERENCES board(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  agent        TEXT NOT NULL DEFAULT '',
  actions_json TEXT NOT NULL DEFAULT '{}',
  UNIQUE(board_id, name)
);

CREATE TABLE IF NOT EXISTS suite (
  id          INTEGER PRIMARY KEY,
  name        TEXT NOT NULL UNIQUE,
  title       TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  select_json TEXT NOT NULL DEFAULT '{}',
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Key/value settings (admin credentials, etc.).
CREATE TABLE IF NOT EXISTS setting (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

-- Manual tests are Markdown repo artifacts (atf_checks/<model>/<vector>/<id>.md), discovered
-- like code checks — not stored in the DB.

-- A Test Plan = a Suite linked to a bench/board (what to run + where). Executable, named.
CREATE TABLE IF NOT EXISTS test_plan (
  name        TEXT PRIMARY KEY,
  suite       TEXT NOT NULL DEFAULT '',
  bench       TEXT NOT NULL DEFAULT '',
  board       TEXT NOT NULL DEFAULT '',
  mgmt_backend TEXT NOT NULL DEFAULT 'docker',
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A bench references shared inventory resources by name + owns the per-bench wiring.
CREATE TABLE IF NOT EXISTS bench_agent (
  bench_id   INTEGER NOT NULL REFERENCES bench(id) ON DELETE CASCADE,
  agent_name TEXT NOT NULL,
  PRIMARY KEY (bench_id, agent_name)
);
CREATE TABLE IF NOT EXISTS bench_board (
  bench_id   INTEGER NOT NULL REFERENCES bench(id) ON DELETE CASCADE,
  board_name TEXT NOT NULL,
  PRIMARY KEY (bench_id, board_name)
);
-- A board's driver instances on a bench. `vector` is the driver ALIAS (the test-context key:
-- console/craft/mgmt/oob/…); `driver_name` names the inv_driver entity (type serial|ip); the
-- per-board params (agent, ip, device, baud) live in config_json.
CREATE TABLE IF NOT EXISTS bench_vector (
  bench_id    INTEGER NOT NULL REFERENCES bench(id) ON DELETE CASCADE,
  board_name  TEXT NOT NULL,
  vector      TEXT NOT NULL,                 -- driver alias (ctx key)
  driver_name TEXT NOT NULL DEFAULT '',      -- which inv_driver entity
  config_json TEXT NOT NULL DEFAULT '{}',    -- per-board params
  PRIMARY KEY (bench_id, board_name, vector)
);
-- A board's node-action instances on a bench. `name` is the instance label; `action_name` names
-- the inv_action entity; `agent` + `actions_json` (per-signal commands) are the bench wiring.
CREATE TABLE IF NOT EXISTS bench_hook (
  bench_id     INTEGER NOT NULL REFERENCES bench(id) ON DELETE CASCADE,
  board_name   TEXT NOT NULL,
  name         TEXT NOT NULL,
  action_name  TEXT NOT NULL DEFAULT '',     -- which inv_action entity
  agent        TEXT NOT NULL DEFAULT '',
  actions_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (bench_id, board_name, name)
);
-- Board credentials scoped to a bench (creds are bench wiring, not an inventory-board property).
CREATE TABLE IF NOT EXISTS bench_board_cred (
  bench_id   INTEGER NOT NULL REFERENCES bench(id) ON DELETE CASCADE,
  board_name TEXT NOT NULL,
  role       TEXT NOT NULL,
  username   TEXT NOT NULL DEFAULT '',
  secret_ref TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (bench_id, board_name, role)
);

-- Shared inventory of bench resources (public — any user edits). Benches import these by name;
-- the per-bench wiring (vectors/hooks) lives with the bench, not here.
CREATE TABLE IF NOT EXISTS inv_agent (
  name           TEXT PRIMARY KEY,
  platform       TEXT NOT NULL DEFAULT 'linux',
  host           TEXT NOT NULL DEFAULT '',
  ssh_user       TEXT NOT NULL DEFAULT '',
  ssh_secret_ref TEXT NOT NULL DEFAULT '',
  comments       TEXT NOT NULL DEFAULT '',
  last_editor    TEXT NOT NULL DEFAULT '',
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS inv_board (
  name        TEXT PRIMARY KEY,
  model       TEXT NOT NULL DEFAULT '',
  serial      TEXT NOT NULL DEFAULT '',
  mgmt_ip      TEXT,
  mgmt_prefix  INTEGER,
  mgmt_gateway TEXT,
  comments    TEXT NOT NULL DEFAULT '',
  last_editor TEXT NOT NULL DEFAULT '',
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS inv_board_cred (
  board_name TEXT NOT NULL REFERENCES inv_board(name) ON DELETE CASCADE,
  role       TEXT NOT NULL,
  username   TEXT NOT NULL DEFAULT '',
  secret_ref TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (board_name, role)
);
-- Reusable DRIVER entities (comm channels). `type` is a built-in kind (serial | ip); `alias` is the
-- default test-context key; `props_json` holds type-default props. A bench instantiates one on a
-- board and parameterizes the rest (agent, ip, device, baud) in bench_vector.config_json.
-- Reusable DRIVER TYPES: name + description + a prop SCHEMA. `props_json` is a list of
-- {name, description}; a bench instantiates the type on a board with an alias + prop values.
-- serial/ip are the built-in, channel-bearing types.
CREATE TABLE IF NOT EXISTS inv_driver (
  name        TEXT PRIMARY KEY,
  description TEXT NOT NULL DEFAULT '',
  type        TEXT NOT NULL DEFAULT 'ip',    -- (vestigial) channel is chosen by name: serial | else ip
  alias       TEXT NOT NULL DEFAULT '',
  props_json  TEXT NOT NULL DEFAULT '[]',    -- [{name, description}, …]
  last_editor TEXT NOT NULL DEFAULT '',
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Reusable NODE-ACTION entities. name + description + signals; `signals_json` is a list of
-- {name, description}. A bench wires agent + per-signal command in bench_hook.
CREATE TABLE IF NOT EXISTS inv_action (
  name         TEXT PRIMARY KEY,
  description  TEXT NOT NULL DEFAULT '',
  signals_json TEXT NOT NULL DEFAULT '[]',   -- [{name, description}, …]
  last_editor  TEXT NOT NULL DEFAULT '',
  updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Upstream check repositories the server tries to sync (git clone/fetch). If a repo can't be
-- reached/authorized, sync fails gracefully (status='error') and it contributes no checks —
-- the server then depends on connected agents' checks.
CREATE TABLE IF NOT EXISTS check_source (
  name         TEXT PRIMARY KEY,
  url          TEXT NOT NULL DEFAULT '',   -- a git URL (kind=git) or a server-side directory (kind=path)
  ref          TEXT NOT NULL DEFAULT 'main',
  kind         TEXT NOT NULL DEFAULT 'git',-- 'git' (server clones/syncs) | 'path' (server-local dir, no clone)
  enabled      INTEGER NOT NULL DEFAULT 1,
  token_enc    TEXT NOT NULL DEFAULT '',  -- encrypted auth token for private HTTPS repos (GitHub PAT etc.)
  last_sync    TEXT,
  last_status  TEXT,                 -- 'ok' | 'error' | 'disabled' | NULL (never)
  last_message TEXT,
  last_commit  TEXT,                 -- sha1 of the loaded point (provenance)
  last_sync_by TEXT,                 -- the logged-in user who triggered the sync (provenance)
  checkout     TEXT                  -- local path once cloned (kind=git) or the dir itself (kind=path)
);

-- Supported board models (admin-managed). `slug` = the check-namespace this model maps to
-- (atf_checks/<slug>/…); several models may share one slug (e.g. Router-X, Router-X Lite → router-x).
CREATE TABLE IF NOT EXISTS board_model (
  name        TEXT PRIMARY KEY,
  description TEXT NOT NULL DEFAULT '',
  slug        TEXT NOT NULL DEFAULT ''
);

-- A catalog = a requirement namespace/framework (e.g. `acme`). May be empty (title only).
CREATE TABLE IF NOT EXISTS catalog (
  framework TEXT PRIMARY KEY,
  title     TEXT NOT NULL DEFAULT ''
);

-- Requirement catalogs (framework:code, e.g. acme:G.2). Seeded from requirements/*.yaml.
CREATE TABLE IF NOT EXISTS requirement (
  id        INTEGER PRIMARY KEY,
  framework TEXT NOT NULL,
  code      TEXT NOT NULL,
  title     TEXT NOT NULL DEFAULT '',
  descr     TEXT NOT NULL DEFAULT '',
  verify    TEXT NOT NULL DEFAULT '',
  priority  TEXT,
  ordinal   INTEGER NOT NULL DEFAULT 0,
  UNIQUE(framework, code)
);

-- One row per Test Plan execution (a report). Owned by the user who ran it; private by default,
-- can be made public to other users. The actual records live in runs.jsonl keyed by run_id.
CREATE TABLE IF NOT EXISTS report (
  run_id     TEXT PRIMARY KEY,
  owner      TEXT NOT NULL DEFAULT '',
  suite      TEXT NOT NULL DEFAULT '',
  bench      TEXT NOT NULL DEFAULT '',
  board      TEXT NOT NULL DEFAULT '',
  ts         TEXT NOT NULL DEFAULT (datetime('now')),
  visibility TEXT NOT NULL DEFAULT 'private',    -- private | public
  counts_json TEXT NOT NULL DEFAULT '{}',
  select_json TEXT NOT NULL DEFAULT '{}',        -- snapshot of the suite map at run time (roll-up + provenance)
  meta_json   TEXT NOT NULL DEFAULT '{}'         -- run-meta snapshot: bench under test + check-source versions as loaded
);

-- App users (local user/password auth hosted by the framework). is_admin gates admin functions.
CREATE TABLE IF NOT EXISTS app_user (
  username    TEXT PRIMARY KEY,
  pw_hash     TEXT NOT NULL,
  is_admin    INTEGER NOT NULL DEFAULT 0,
  agent_token TEXT NOT NULL DEFAULT '',      -- per-user agent enrollment token (private to the user)
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Per-bench secret values, encrypted with APP_SECRET (never plaintext at rest).
CREATE TABLE IF NOT EXISTS secret (
  id        INTEGER PRIMARY KEY,
  bench_id  INTEGER NOT NULL REFERENCES bench(id) ON DELETE CASCADE,
  ref       TEXT NOT NULL,
  value_enc TEXT NOT NULL,
  UNIQUE(bench_id, ref)
);
