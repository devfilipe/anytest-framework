"""Repository — the only code that touches SQL. CRUD for benches/suites/secrets plus
YAML import/export (round-trips the file format) and a bridge that builds an
`inventory.Bench` (decrypting secrets in memory) so the runner can execute against a
DB-backed config. Swapping SQLite→Postgres means changing db.py, not this file's shape.

The connection is shared across FastAPI's threadpool, so every public method serialises on
an RLock (SQLite has no cross-thread cursor safety).
"""
from __future__ import annotations

import json
import re
import threading


class Repo:
    def __init__(self, con, cipher):
        self.con = con
        self.cipher = cipher
        self._lock = threading.RLock()

    # --- helpers ---
    def _one(self, sql, args=()):
        return self.con.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        return self.con.execute(sql, args).fetchall()

    def _bench_id(self, name, create=True):
        r = self._one("SELECT id FROM bench WHERE name=?", (name,))
        if r:
            return r["id"]
        if not create:
            return None
        cur = self.con.execute("INSERT INTO bench(name) VALUES(?)", (name,))
        self.con.commit()
        return cur.lastrowid

    # --- benches ---
    def list_benches(self) -> list[dict]:
        with self._lock:
            out = []
            for b in self._all("SELECT * FROM bench WHERE name != '__inventory__' ORDER BY name"):
                c = lambda t: self._one(f"SELECT COUNT(*) n FROM {t} WHERE bench_id=?", (b["id"],))["n"]  # noqa: E731
                # distinct board-model slugs this bench supports (for suite↔bench matching)
                raw = [r["model"] for r in self._all(
                    "SELECT DISTINCT ib.model model FROM bench_board bb "
                    "JOIN inv_board ib ON ib.name=bb.board_name WHERE bb.bench_id=? AND ib.model!=''",
                    (b["id"],))]
                models = sorted({self.board_model_slug(m) or re.sub(r"[^a-z0-9]+", "_", m.lower()).strip("_")
                                 for m in raw})
                out.append({"name": b["name"], "agents": c("bench_agent"), "boards": c("bench_board"),
                            "secrets": c("secret"), "updated_at": b["updated_at"], "models": models})
            return out

    def get_bench(self, name) -> dict | None:
        """Reconstruct the bench data shape by joining the inventory (agents/boards it imports)
        with the bench's own wiring (vectors/hooks). Same shape as before, so the editor and
        inventory.parse() are unchanged."""
        with self._lock:
            b = self._one("SELECT id FROM bench WHERE name=?", (name,))
            if not b:
                return None
            bid = b["id"]
            agents = {}
            for ln in self._all("SELECT agent_name FROM bench_agent WHERE bench_id=? ORDER BY agent_name", (bid,)):
                a = self._one("SELECT * FROM inv_agent WHERE name=?", (ln["agent_name"],))
                if a:
                    agents[a["name"]] = {"platform": a["platform"], "host": a["host"],
                                         "ssh": {"user": a["ssh_user"], "password_ref": a["ssh_secret_ref"]}}
            boards = []
            for ln in self._all("SELECT board_name FROM bench_board WHERE bench_id=? ORDER BY board_name", (bid,)):
                bd = self._one("SELECT * FROM inv_board WHERE name=?", (ln["board_name"],))
                if not bd:
                    continue
                # creds are bench-scoped wiring, not an inventory-board property
                creds = {c["role"]: {"user": c["username"], "password_ref": c["secret_ref"]}
                         for c in self._all("SELECT * FROM bench_board_cred WHERE bench_id=? AND board_name=? ORDER BY role",
                                            (bid, bd["name"]))}
                # driver instances: bench_vector (alias `vector` + `driver_name`=the driver TYPE +
                # per-board prop values in config_json). The type IS the driver_name; it selects the
                # channel (serial | ip) at run time.
                drivers = {}
                for v in self._all("SELECT * FROM bench_vector WHERE bench_id=? AND board_name=?", (bid, bd["name"])):
                    cfg = json.loads(v["config_json"] or "{}")
                    typ = v["driver_name"] or cfg.get("type") or "ip"
                    drivers[v["vector"]] = {"type": typ, "driver_name": v["driver_name"], **cfg}
                actions = {h["name"]: {"action_name": h["action_name"], "agent": h["agent"],
                                       "signals": json.loads(h["actions_json"])}
                           for h in self._all("SELECT * FROM bench_hook WHERE bench_id=? AND board_name=?", (bid, bd["name"]))}
                d = {"name": bd["name"], "model": bd["model"], "serial": bd["serial"],
                     "creds": creds, "drivers": drivers}
                if actions:
                    d["actions"] = actions
                boards.append(d)
            return {"agents": agents, "boards": boards}

    def _get_bench_legacy(self, bid: int) -> dict:
        """Read a bench from the OLD per-bench tables (for one-time migration to the inventory)."""
        agents = {a["name"]: {"platform": a["platform"], "host": a["host"],
                              "ssh": {"user": a["ssh_user"], "password_ref": a["ssh_secret_ref"]}}
                  for a in self._all("SELECT * FROM agent WHERE bench_id=? ORDER BY name", (bid,))}
        boards = []
        for bd in self._all("SELECT * FROM board WHERE bench_id=? ORDER BY id", (bid,)):
            creds = {c["role"]: {"user": c["username"], "password_ref": c["secret_ref"]}
                     for c in self._all("SELECT * FROM board_cred WHERE board_id=? ORDER BY role", (bd["id"],))}
            vectors = {v["vector"]: json.loads(v["config_json"])
                       for v in self._all("SELECT * FROM board_vector WHERE board_id=?", (bd["id"],))}
            hooks = {h["name"]: {"agent": h["agent"], "actions": json.loads(h["actions_json"])}
                     for h in self._all("SELECT * FROM board_hook WHERE board_id=?", (bd["id"],))}
            d = {"name": bd["name"], "model": bd["model"], "serial": bd["serial"],
                 "mgmt": {"ip": bd["mgmt_ip"], "prefix": bd["mgmt_prefix"], "gateway": bd["mgmt_gateway"]},
                 "creds": creds, "vectors": vectors}
            if hooks:
                d["hooks"] = hooks
            boards.append(d)
        return {"agents": agents, "boards": boards}

    def upsert_bench(self, name, data: dict) -> None:
        """A bench IMPORTS inventory agents/boards + owns the wiring. Decomposes the data shape:
        agents/boards are upserted into the shared inventory (so benches share them) and linked;
        vectors/hooks are stored per-bench."""
        with self._lock:
            cur = self.con.cursor()
            row = self._one("SELECT id FROM bench WHERE name=?", (name,))
            if row:
                bid = row["id"]
                cur.execute("UPDATE bench SET updated_at=datetime('now') WHERE id=?", (bid,))
                for t in ("bench_agent", "bench_board", "bench_vector", "bench_hook", "bench_board_cred"):
                    cur.execute(f"DELETE FROM {t} WHERE bench_id=?", (bid,))
            else:
                cur.execute("INSERT INTO bench(name) VALUES(?)", (name,))
                bid = cur.lastrowid
            for an, a in (data.get("agents") or {}).items():
                ssh = a.get("ssh") or {}
                self._inv_agent_cur(cur, an, a.get("platform", "linux"), a.get("host", ""),
                                    ssh.get("user", ""), ssh.get("password_ref", ""))
                cur.execute("INSERT OR IGNORE INTO bench_agent(bench_id,agent_name) VALUES(?,?)", (bid, an))
            for bd in (data.get("boards") or []):
                # inventory board is name/model/serial only — creds/drivers are bench wiring
                self._inv_board_cur(cur, bd["name"], {"model": bd.get("model", ""), "serial": bd.get("serial", "")})
                cur.execute("INSERT OR IGNORE INTO bench_board(bench_id,board_name) VALUES(?,?)", (bid, bd["name"]))
                for r, c in (bd.get("creds") or {}).items():
                    cur.execute("INSERT OR REPLACE INTO bench_board_cred(bench_id,board_name,role,username,secret_ref) "
                                "VALUES(?,?,?,?,?)", (bid, bd["name"], r, (c or {}).get("user", ""), (c or {}).get("password_ref", "")))
                for alias, cfg in (bd.get("drivers") or {}).items():
                    cfg = dict(cfg or {})
                    dn = cfg.pop("driver_name", "")
                    cfg.pop("type", None)   # type belongs to the inv_driver entity, not the per-board config
                    cur.execute("INSERT OR REPLACE INTO bench_vector(bench_id,board_name,vector,driver_name,config_json) "
                                "VALUES(?,?,?,?,?)", (bid, bd["name"], alias, dn, json.dumps(cfg)))
                for label, a in (bd.get("actions") or {}).items():
                    sigs = {("on" if k is True else "off" if k is False else str(k)): v
                            for k, v in (a.get("signals") or a.get("actions") or {}).items()}
                    cur.execute("INSERT OR REPLACE INTO bench_hook(bench_id,board_name,name,action_name,agent,actions_json) "
                                "VALUES(?,?,?,?,?,?)", (bid, bd["name"], label, a.get("action_name", label),
                                                       a.get("agent", ""), json.dumps(sigs)))
            self.con.commit()

    def migrate_benches_to_inventory(self) -> int:
        """One-time: move benches stored in the OLD per-bench tables into the inventory + link
        tables. Runs only for benches not yet linked. Returns how many were migrated."""
        with self._lock:
            try:
                has_old = self._one("SELECT COUNT(*) n FROM agent")["n"] or \
                    self._one("SELECT COUNT(*) n FROM board")["n"]
            except Exception:
                return 0
            if not has_old:
                return 0
            n = 0
            for b in self._all("SELECT id,name FROM bench"):
                linked = self._one("SELECT COUNT(*) n FROM bench_agent WHERE bench_id=?", (b["id"],))["n"] + \
                    self._one("SELECT COUNT(*) n FROM bench_board WHERE bench_id=?", (b["id"],))["n"]
                if linked:
                    continue
                data = self._get_bench_legacy(b["id"])
                if data.get("agents") or data.get("boards"):
                    self.upsert_bench(b["name"], data)
                    n += 1
            return n

    def delete_bench(self, name) -> None:
        with self._lock:
            self.con.execute("DELETE FROM bench WHERE name=?", (name,))
            self.con.commit()

    # --- secrets (encrypted at rest) ---
    def set_secrets(self, bench, secrets: dict, replace=False) -> None:
        with self._lock:
            bid = self._bench_id(bench)
            cur = self.con.cursor()
            if replace:
                cur.execute("DELETE FROM secret WHERE bench_id=?", (bid,))
            for ref, val in (secrets or {}).items():
                cur.execute("INSERT INTO secret(bench_id,ref,value_enc) VALUES(?,?,?) "
                            "ON CONFLICT(bench_id,ref) DO UPDATE SET value_enc=excluded.value_enc",
                            (bid, ref, self.cipher.enc(str(val))))
            self.con.commit()

    def delete_secret(self, bench, ref) -> None:
        with self._lock:
            bid = self._bench_id(bench, create=False)
            if bid:
                self.con.execute("DELETE FROM secret WHERE bench_id=? AND ref=?", (bid, ref))
                self.con.commit()

    def secret_refs(self, bench) -> list[str]:
        with self._lock:
            bid = self._bench_id(bench, create=False)
            if not bid:
                return []
            return [r["ref"] for r in self._all("SELECT ref FROM secret WHERE bench_id=? ORDER BY ref", (bid,))]

    def secrets(self, bench, reveal=False) -> dict:
        with self._lock:
            bid = self._bench_id(bench, create=False)
            if not bid:
                return {}
            rows = self._all("SELECT ref,value_enc FROM secret WHERE bench_id=?", (bid,))
            return {r["ref"]: (self.cipher.dec(r["value_enc"]) if reveal else "***") for r in rows}

    # --- suites ---
    def list_suites(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._all(
                "SELECT name,title,description,updated_at FROM suite ORDER BY name")]

    def get_suite(self, name) -> dict | None:
        with self._lock:
            r = self._one("SELECT * FROM suite WHERE name=?", (name,))
            if not r:
                return None
            return {"title": r["title"], "description": r["description"],
                    "select": json.loads(r["select_json"])}

    def upsert_suite(self, name, data: dict) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO suite(name,title,description,select_json) VALUES(?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET title=excluded.title,description=excluded.description,"
                "select_json=excluded.select_json,updated_at=datetime('now')",
                (name, data.get("title", ""), data.get("description", ""),
                 json.dumps(data.get("select") or {})))
            self.con.commit()

    def delete_suite(self, name) -> None:
        with self._lock:
            self.con.execute("DELETE FROM suite WHERE name=?", (name,))
            self.con.commit()

    # --- requirements & catalogs ---
    def upsert_catalog(self, framework: str, title: str = "") -> None:
        with self._lock:
            self.con.execute("INSERT INTO catalog(framework,title) VALUES(?,?) "
                             "ON CONFLICT(framework) DO UPDATE SET title=excluded.title",
                             (framework, title))
            self.con.commit()

    def import_requirements(self, framework: str, parsed: dict, title: str = "") -> None:
        """Replace a framework's requirements. `parsed` = {code: {title,desc,verify,priority}}."""
        with self._lock:
            cur = self.con.cursor()
            cur.execute("INSERT INTO catalog(framework,title) VALUES(?,?) "
                        "ON CONFLICT(framework) DO UPDATE SET title=excluded.title", (framework, title))
            cur.execute("DELETE FROM requirement WHERE framework=?", (framework,))
            for i, (code, m) in enumerate(parsed.items()):
                cur.execute("INSERT INTO requirement(framework,code,title,descr,verify,priority,ordinal) "
                            "VALUES(?,?,?,?,?,?,?)",
                            (framework, code, m.get("title", ""), m.get("desc", ""),
                             m.get("verify", ""), m.get("priority"), i))
            self.con.commit()

    def upsert_requirement(self, framework: str, code: str, meta: dict) -> None:
        with self._lock:
            self.con.execute("INSERT INTO catalog(framework,title) VALUES(?,'') "
                             "ON CONFLICT(framework) DO NOTHING", (framework,))
            row = self._one("SELECT id FROM requirement WHERE framework=? AND code=?", (framework, code))
            if row:
                self.con.execute("UPDATE requirement SET title=?,descr=?,verify=?,priority=? WHERE id=?",
                                 (meta.get("title", ""), meta.get("desc", ""), meta.get("verify", ""),
                                  meta.get("priority"), row["id"]))
            else:
                mx = self._one("SELECT COALESCE(MAX(ordinal),-1) m FROM requirement WHERE framework=?",
                               (framework,))["m"]
                self.con.execute("INSERT INTO requirement(framework,code,title,descr,verify,priority,ordinal) "
                                 "VALUES(?,?,?,?,?,?,?)",
                                 (framework, code, meta.get("title", ""), meta.get("desc", ""),
                                  meta.get("verify", ""), meta.get("priority"), mx + 1))
            self.con.commit()

    def delete_requirement(self, framework: str, code: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM requirement WHERE framework=? AND code=?", (framework, code))
            self.con.commit()

    def delete_framework(self, framework: str) -> None:
        with self._lock:
            self.con.execute("DELETE FROM requirement WHERE framework=?", (framework,))
            self.con.execute("DELETE FROM catalog WHERE framework=?", (framework,))
            self.con.commit()

    def frameworks(self) -> list[dict]:
        with self._lock:
            titles = {r["framework"]: r["title"] for r in self._all("SELECT framework,title FROM catalog")}
            names = set(titles) | {r["framework"] for r in
                                   self._all("SELECT DISTINCT framework FROM requirement")}
            out = []
            for fw in sorted(names):
                n = self._one("SELECT COUNT(*) n FROM requirement WHERE framework=?", (fw,))["n"]
                out.append({"framework": fw, "title": titles.get(fw, ""), "count": n})
            return out

    def list_requirements(self, framework=None) -> list[dict]:
        with self._lock:
            if framework:
                rows = self._all("SELECT * FROM requirement WHERE framework=? ORDER BY ordinal", (framework,))
            else:
                rows = self._all("SELECT * FROM requirement ORDER BY framework, ordinal")
            from atf.core.requirements import requirement_sha
            out = []
            for r in rows:
                row = {"id": f"{r['framework']}:{r['code']}", "framework": r["framework"],
                       "code": r["code"], "title": r["title"], "desc": r["descr"],
                       "verify": r["verify"], "priority": r["priority"]}
                out.append({**row, "sha": requirement_sha(row)})
            return out

    # --- settings & admin auth ---
    def get_setting(self, key, default=""):
        with self._lock:
            r = self._one("SELECT value FROM setting WHERE key=?", (key,))
            return r["value"] if r else default

    def set_setting(self, key, value):
        with self._lock:
            self.con.execute("INSERT INTO setting(key,value) VALUES(?,?) "
                             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self.con.commit()

    @staticmethod
    def _hash(pw):
        import hashlib
        return hashlib.sha256(("atf$" + pw).encode()).hexdigest()

    def ensure_admin(self):
        """Seed the default admin/admin user on a fresh store (migrates the old settings-based admin)."""
        with self._lock:
            if self._one("SELECT COUNT(*) c FROM app_user")["c"] == 0:
                un = self.get_setting("admin_user", "admin")            # carry over a customized admin
                ph = self.get_setting("admin_pass", self._hash("admin"))
                self.con.execute("INSERT INTO app_user(username,pw_hash,is_admin) VALUES(?,?,1)", (un, ph))
                self.con.commit()

    # --- app users (local auth; is_admin gates admin functions) ---
    def verify_user(self, username, pw) -> dict | None:
        with self._lock:
            r = self._one("SELECT username,is_admin FROM app_user WHERE username=? AND pw_hash=?",
                          (username, self._hash(pw)))
            return {"username": r["username"], "is_admin": bool(r["is_admin"])} if r else None

    def list_users(self) -> list[dict]:
        with self._lock:
            return [{"username": r["username"], "is_admin": bool(r["is_admin"]), "created_at": r["created_at"]}
                    for r in self._all("SELECT username,is_admin,created_at FROM app_user ORDER BY username")]

    def upsert_user(self, username, is_admin, pw=None):
        with self._lock:
            if self._one("SELECT username FROM app_user WHERE username=?", (username,)):
                self.con.execute("UPDATE app_user SET is_admin=? WHERE username=?", (1 if is_admin else 0, username))
                if pw:
                    self.con.execute("UPDATE app_user SET pw_hash=? WHERE username=?", (self._hash(pw), username))
            else:
                self.con.execute("INSERT INTO app_user(username,pw_hash,is_admin) VALUES(?,?,?)",
                                 (username, self._hash(pw or ""), 1 if is_admin else 0))
            self.con.commit()

    def set_user_password(self, username, pw):
        with self._lock:
            self.con.execute("UPDATE app_user SET pw_hash=? WHERE username=?", (self._hash(pw), username))
            self.con.commit()

    def delete_user(self, username):
        with self._lock:
            self.con.execute("DELETE FROM app_user WHERE username=?", (username,))
            self.con.commit()

    def admin_count(self) -> int:
        with self._lock:
            return self._one("SELECT COUNT(*) c FROM app_user WHERE is_admin=1")["c"]

    def user_agent_token(self, username) -> str:           # this user's enrollment token (creates on demand)
        import secrets as _s
        with self._lock:
            r = self._one("SELECT agent_token FROM app_user WHERE username=?", (username,))
            if r and r["agent_token"]:
                return r["agent_token"]
            tok = _s.token_hex(16)
            self.con.execute("UPDATE app_user SET agent_token=? WHERE username=?", (tok, username))
            self.con.commit()
            return tok

    def rotate_user_agent_token(self, username) -> str:
        import secrets as _s
        tok = _s.token_hex(16)
        with self._lock:
            self.con.execute("UPDATE app_user SET agent_token=? WHERE username=?", (tok, username))
            self.con.commit()
        return tok

    def user_of_agent_token(self, token) -> str | None:    # which user owns this enrollment token
        if not token:
            return None
        with self._lock:
            r = self._one("SELECT username FROM app_user WHERE agent_token=?", (token,))
            return r["username"] if r else None

    def verify_admin(self, user, pw) -> bool:              # kept for callers that need admin specifically
        u = self.verify_user(user, pw)
        return bool(u and u["is_admin"])

    # --- reports (one per Test Plan execution; owner + visibility) ---
    @staticmethod
    def _report_row(r) -> dict:
        return {"run_id": r["run_id"], "owner": r["owner"], "suite": r["suite"],
                "bench": r["bench"], "board": r["board"], "ts": r["ts"],
                "visibility": r["visibility"], "counts": json.loads(r["counts_json"] or "{}"),
                "select": json.loads((r["select_json"] if "select_json" in r.keys() else "") or "{}"),
                "meta": json.loads((r["meta_json"] if "meta_json" in r.keys() else "") or "{}")}

    def add_report(self, run_id, owner, suite, bench, board, counts, visibility="private", ts=None,
                   select=None, meta=None):
        with self._lock:
            self.con.execute(
                "INSERT INTO report(run_id,owner,suite,bench,board,ts,visibility,counts_json,select_json,meta_json) "
                "VALUES(?,?,?,?,?,COALESCE(?,datetime('now')),?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET owner=excluded.owner,suite=excluded.suite,"
                "bench=excluded.bench,board=excluded.board,counts_json=excluded.counts_json,"
                "select_json=excluded.select_json,meta_json=excluded.meta_json",
                (run_id, owner or "", suite or "", bench or "", board or "", ts, visibility,
                 json.dumps(counts or {}), json.dumps(select or {}), json.dumps(meta or {})))
            self.con.commit()

    def list_reports(self, viewer, is_admin=False) -> list[dict]:
        with self._lock:
            if is_admin:
                rows = self._all("SELECT * FROM report ORDER BY ts DESC")
            else:
                rows = self._all("SELECT * FROM report WHERE owner=? OR visibility='public' ORDER BY ts DESC",
                                 (viewer,))
            return [self._report_row(r) for r in rows]

    def get_report(self, run_id, viewer=None, is_admin=False) -> dict | None:
        with self._lock:
            r = self._one("SELECT * FROM report WHERE run_id=?", (run_id,))
        if not r:
            return None
        row = self._report_row(r)
        if is_admin or row["owner"] == viewer or row["visibility"] == "public":
            return row
        return None                                        # private, not yours

    def set_report_visibility(self, run_id, visibility, viewer, is_admin=False) -> bool:
        with self._lock:
            r = self._one("SELECT owner FROM report WHERE run_id=?", (run_id,))
            if not r or (r["owner"] != viewer and not is_admin):
                return False
            self.con.execute("UPDATE report SET visibility=? WHERE run_id=?",
                             ("public" if visibility == "public" else "private", run_id))
            self.con.commit()
            return True

    def delete_report(self, run_id, viewer, is_admin=False) -> bool:
        with self._lock:
            r = self._one("SELECT owner FROM report WHERE run_id=?", (run_id,))
            if not r or (r["owner"] != viewer and not is_admin):
                return False
            self.con.execute("DELETE FROM report WHERE run_id=?", (run_id,))
            self.con.commit()
            return True

    # --- test plans (suite + bench/board) ---
    def list_test_plans(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._all(
                "SELECT name,suite,bench,board,mgmt_backend,updated_at FROM test_plan ORDER BY name")]

    def get_test_plan(self, name) -> dict | None:
        with self._lock:
            r = self._one("SELECT name,suite,bench,board,mgmt_backend FROM test_plan WHERE name=?", (name,))
            return dict(r) if r else None

    def upsert_test_plan(self, name, d: dict) -> None:
        with self._lock:
            self.con.execute(
                "INSERT INTO test_plan(name,suite,bench,board,mgmt_backend) VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET suite=excluded.suite,bench=excluded.bench,"
                "board=excluded.board,mgmt_backend=excluded.mgmt_backend,updated_at=datetime('now')",
                (name, d.get("suite", ""), d.get("bench", ""), d.get("board", ""),
                 d.get("mgmt_backend", "docker")))
            self.con.commit()

    def delete_test_plan(self, name) -> None:
        with self._lock:
            self.con.execute("DELETE FROM test_plan WHERE name=?", (name,))
            self.con.commit()

    # --- board models ---
    def list_board_models(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._all(
                "SELECT name,description,slug FROM board_model ORDER BY name")]

    def upsert_board_model(self, name, description="", slug=""):
        with self._lock:
            self.con.execute(
                "INSERT INTO board_model(name,description,slug) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET description=excluded.description, slug=excluded.slug",
                (name, description, slug))
            self.con.commit()

    # --- check source repositories (upstream) ---
    def list_check_sources(self) -> list[dict]:
        # never exposes the raw token — only whether one is set (has_token)
        with self._lock:
            out = []
            for r in self._all(
                "SELECT name,url,ref,kind,enabled,token_enc,last_sync,last_status,last_message,"
                    "last_commit,last_sync_by,checkout FROM check_source ORDER BY name"):
                d = dict(r)
                d["has_token"] = bool(d.pop("token_enc", ""))
                out.append(d)
            return out

    def get_check_source(self, name: str) -> dict | None:
        with self._lock:
            r = self.con.execute(
                "SELECT name,url,ref,kind,enabled,token_enc,last_sync,last_status,last_message,"
                "last_commit,last_sync_by,checkout FROM check_source WHERE name=?", (name,)).fetchone()
            if not r:
                return None
            d = dict(r)
            d["has_token"] = bool(d.pop("token_enc", ""))
            return d

    def get_check_source_token(self, name: str) -> str:
        """Decrypted auth token for sync (empty if none). Server-side only — never sent to a client."""
        with self._lock:
            r = self.con.execute("SELECT token_enc FROM check_source WHERE name=?", (name,)).fetchone()
            return self.cipher.dec(r["token_enc"]) if r and r["token_enc"] else ""

    def upsert_check_source(self, name, url, ref="main", enabled=1, token=None, kind="git"):
        """`token=None` keeps the existing token (edit without re-entering it); `token=""` clears it;
        a non-empty token is stored encrypted at rest. `kind` = 'git' | 'path'."""
        with self._lock:
            self.con.execute(
                "INSERT INTO check_source(name,url,ref,kind,enabled) VALUES(?,?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET url=excluded.url, ref=excluded.ref, "
                "kind=excluded.kind, enabled=excluded.enabled",
                (name, url, ref, kind if kind in ("git", "path") else "git", 1 if enabled else 0))
            if token is not None:
                self.con.execute("UPDATE check_source SET token_enc=? WHERE name=?",
                                 (self.cipher.enc(token) if token else "", name))
            self.con.commit()

    def delete_check_source(self, name):
        with self._lock:
            self.con.execute("DELETE FROM check_source WHERE name=?", (name,))
            self.con.commit()

    def set_check_source_status(self, name, status, message="", checkout=None, ts="",
                                commit=None, by=None):
        with self._lock:
            self.con.execute(
                "UPDATE check_source SET last_status=?, last_message=?, last_sync=?, "
                "checkout=COALESCE(?,checkout), last_commit=COALESCE(?,last_commit), "
                "last_sync_by=COALESCE(?,last_sync_by) WHERE name=?",
                (status, message, ts, checkout, commit, by, name))
            self.con.commit()

    def clear_check_source_sync(self, name) -> dict | None:
        """Forget a repo's synced state (keeps the repo config so it can be re-synced). Returns
        `{checkout, kind}` so the caller can remove a *server-cloned* dir — never a `path` source's
        own directory."""
        with self._lock:
            r = self.con.execute("SELECT checkout,kind FROM check_source WHERE name=?", (name,)).fetchone()
            self.con.execute("UPDATE check_source SET last_status=NULL, last_message='', "
                             "last_commit=NULL, checkout=NULL WHERE name=?", (name,))
            self.con.commit()
            return {"checkout": r["checkout"], "kind": r["kind"]} if r else None

    # --- backup / restore (whole-store snapshot for server migration) ---
    def backup(self, dest):
        """Write a consistent snapshot of the entire config store to `dest` (online, no downtime)."""
        from atf.store import backup as _bk
        with self._lock:
            self.con.execute("PRAGMA wal_checkpoint(PASSIVE)")
            return _bk.snapshot(self.con, dest)

    def restore(self, src) -> None:
        """Replace the store's contents with the snapshot at `src`, in place. Disruptive: every
        bench/suite/secret/user is overwritten. Secrets decrypt only under the same APP_SECRET."""
        from atf.store import backup as _bk
        with self._lock:
            _bk.restore(self.con, src)

    # --- shared inventory (agents + boards; benches import these by name) ---
    def list_inv_agents(self) -> list[dict]:
        with self._lock:
            rows = [dict(r) for r in self._all(
                "SELECT name,platform,host,ssh_user,ssh_secret_ref,last_editor,updated_at "
                "FROM inv_agent ORDER BY name")]
            for a in rows:
                a["benches"] = [x["name"] for x in self._all(
                    "SELECT b.name FROM bench_agent ba JOIN bench b ON b.id=ba.bench_id "
                    "WHERE ba.agent_name=? ORDER BY b.name", (a["name"],))]
            return rows

    @staticmethod
    def _inv_agent_cur(cur, name, platform="linux", host="", ssh_user="", ssh_secret_ref="", editor=""):
        cur.execute(
            "INSERT INTO inv_agent(name,platform,host,ssh_user,ssh_secret_ref,last_editor,updated_at) "
            "VALUES(?,?,?,?,?,?,datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET platform=excluded.platform, host=excluded.host, "
            "ssh_user=excluded.ssh_user, ssh_secret_ref=excluded.ssh_secret_ref, "
            "last_editor=excluded.last_editor, updated_at=datetime('now')",
            (name, platform or "linux", host, ssh_user, ssh_secret_ref, editor))

    def upsert_inv_agent(self, name, platform="linux", host="", ssh_user="", ssh_secret_ref="", editor=""):
        with self._lock:
            self._inv_agent_cur(self.con.cursor(), name, platform, host, ssh_user, ssh_secret_ref, editor)
            self.con.commit()

    def delete_inv_agent(self, name):
        with self._lock:
            self.con.execute("DELETE FROM inv_agent WHERE name=?", (name,))
            self.con.commit()

    def list_inv_boards(self) -> list[dict]:
        with self._lock:
            boards = [dict(r) for r in self._all(
                "SELECT name,model,serial,last_editor,updated_at FROM inv_board ORDER BY name")]
            for b in boards:
                b["benches"] = [x["name"] for x in self._all(
                    "SELECT b2.name FROM bench_board bb JOIN bench b2 ON b2.id=bb.bench_id "
                    "WHERE bb.board_name=? ORDER BY b2.name", (b["name"],))]
            return boards

    @staticmethod
    def _inv_board_cur(cur, name, data: dict, editor=""):
        # inventory board = name/model/serial only. mgmt/creds are bench wiring now; the legacy
        # inv_board.mgmt_* columns are left untouched (ignored) for backward-compatible restores.
        cur.execute(
            "INSERT INTO inv_board(name,model,serial,last_editor,updated_at) "
            "VALUES(?,?,?,?,datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET model=excluded.model, serial=excluded.serial, "
            "last_editor=excluded.last_editor, updated_at=datetime('now')",
            (name, data.get("model", ""), data.get("serial", ""), editor))

    def upsert_inv_board(self, name, data: dict, editor=""):
        with self._lock:
            self._inv_board_cur(self.con.cursor(), name, data, editor)
            self.con.commit()

    def delete_inv_board(self, name):
        with self._lock:
            self.con.execute("DELETE FROM inv_board WHERE name=?", (name,))
            self.con.commit()

    # --- reusable driver entities (comm channels: serial | ip) ---
    def list_inv_drivers(self) -> list[dict]:
        with self._lock:
            out = []
            for r in self._all("SELECT name,description,props_json,last_editor,updated_at FROM inv_driver ORDER BY name"):
                d = dict(r)
                d["props"] = json.loads(d.pop("props_json") or "[]")   # [{name, description}, …]
                out.append(d)
            return out

    def upsert_inv_driver(self, name, description="", props=None, editor=""):
        """A driver TYPE = name + description + a prop schema (list of {name, description}). A bench
        instantiates it on a board with an alias + values. serial/ip are the built-in, channel-bearing types."""
        with self._lock:
            self.con.execute(
                "INSERT INTO inv_driver(name,description,props_json,last_editor,updated_at) "
                "VALUES(?,?,?,?,datetime('now')) "
                "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
                "props_json=excluded.props_json, last_editor=excluded.last_editor, updated_at=datetime('now')",
                (name, description or "", json.dumps(props or []), editor))
            self.con.commit()

    def delete_inv_driver(self, name):
        with self._lock:
            self.con.execute("DELETE FROM inv_driver WHERE name=?", (name,))
            self.con.commit()

    # --- reusable node-action entities (name + signal list) ---
    def list_inv_actions(self) -> list[dict]:
        with self._lock:
            out = []
            for r in self._all("SELECT name,description,signals_json,last_editor,updated_at FROM inv_action ORDER BY name"):
                d = dict(r)
                d["signals"] = json.loads(d.pop("signals_json") or "[]")   # [{name, description}, …]
                out.append(d)
            return out

    def upsert_inv_action(self, name, description="", signals=None, editor=""):
        with self._lock:
            self.con.execute(
                "INSERT INTO inv_action(name,description,signals_json,last_editor,updated_at) "
                "VALUES(?,?,?,?,datetime('now')) "
                "ON CONFLICT(name) DO UPDATE SET description=excluded.description, "
                "signals_json=excluded.signals_json, last_editor=excluded.last_editor, updated_at=datetime('now')",
                (name, description or "", json.dumps(signals or []), editor))
            self.con.commit()

    def delete_inv_action(self, name):
        with self._lock:
            self.con.execute("DELETE FROM inv_action WHERE name=?", (name,))
            self.con.commit()

    def migrate_drivers_to_inventory(self) -> int:
        """One-time: fold legacy per-bench driver wiring (bench_vector rows with the literal aliases
        console/craft/mgmt and no driver_name) into reusable inv_driver entities, and move creds off
        inv_board into bench_board_cred. Idempotent — only touches rows with driver_name=''. Also
        folds the old inv_board.mgmt_ip and the craft board_ip into the driver's per-board config."""
        # legacy alias → built-in driver TYPE (console is serial; craft/mgmt are ip)
        _MAP = {"console": "serial", "craft": "ip", "mgmt": "ip"}
        with self._lock:
            try:
                rows = self._all("SELECT rowid,bench_id,board_name,vector,config_json "
                                 "FROM bench_vector WHERE driver_name='' OR driver_name IS NULL")
            except Exception:
                return 0
            n = 0
            for r in rows:
                typ = _MAP.get(r["vector"], "ip")
                dn = typ                            # driver_name = the driver TYPE; the alias stays r["vector"]
                cfg = json.loads(r["config_json"] or "{}")
                if typ == "ip" and not cfg.get("ip"):
                    if r["vector"] == "craft" and cfg.get("board_ip"):
                        cfg["ip"] = cfg["board_ip"]
                    elif r["vector"] == "mgmt":
                        b = self._one("SELECT mgmt_ip FROM inv_board WHERE name=?", (r["board_name"],))
                        if b and b["mgmt_ip"]:
                            cfg["ip"] = b["mgmt_ip"]
                self.con.execute("UPDATE bench_vector SET driver_name=?, config_json=? WHERE rowid=?",
                                 (dn, json.dumps(cfg), r["rowid"]))
                n += 1
            # move inv_board_cred → bench_board_cred (per bench that imports the board), once
            for bc in self._all("SELECT bb.bench_id, ic.board_name, ic.role, ic.username, ic.secret_ref "
                                "FROM inv_board_cred ic JOIN bench_board bb ON bb.board_name=ic.board_name"):
                self.con.execute("INSERT OR IGNORE INTO bench_board_cred(bench_id,board_name,role,username,secret_ref) "
                                 "VALUES(?,?,?,?,?)", (bc["bench_id"], bc["board_name"], bc["role"],
                                                       bc["username"], bc["secret_ref"]))
            self.con.commit()
            return n

    def board_model_slug(self, name: str) -> str:
        """The check-namespace slug configured for a board model name (\"\" if none/unknown)."""
        with self._lock:
            r = self.con.execute("SELECT slug FROM board_model WHERE name=?", (name,)).fetchone()
            return (r[0] if r else "") or ""

    def delete_board_model(self, name):
        with self._lock:
            self.con.execute("DELETE FROM board_model WHERE name=?", (name,))
            self.con.commit()

    # --- bridge to the runner ---
    def inventory_bench(self, name):
        """Build an inventory.Bench from the DB (secrets decrypted in memory). Secrets resolve
        from the inventory-level namespace with a per-bench override (bench wins)."""
        from atf.core import inventory
        data = self.get_bench(name)
        if data is None:
            raise KeyError(f"bench not found: {name}")
        secrets = {**self.secrets("__inventory__", reveal=True), **self.secrets(name, reveal=True)}
        return inventory.parse(data, secrets)
