"""atf backend (FastAPI). Serves the SPA + read views over the canonical results store,
pilots runs (SSE progress + web manual-check capture), and manages config — benches, suites
and encrypted secrets — in a SQLite store with YAML import/export. Localhost by default;
deploy behind the docker-compose for the test server.

    atf web            # http://127.0.0.1:8899
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path

import yaml
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from atf.core import requirements as reqmeta
from atf.core.checks import discover as _discover_checks
from atf.core.report import SYMBOL, _cell
from atf.web.agents import AgentHub
from atf.web.locks import ResourceLocks, touched_resources

_discover_checks()  # import all checks from the configured check-source repos (for /api/meta)

_STATIC = Path(__file__).resolve().parent / "static"


# ------------------------- data views -------------------------
def _latest_records(out_root: Path) -> list:
    hist = out_root / "runs.jsonl"
    if hist.exists():
        latest: dict = {}
        for ln in hist.read_text().splitlines():
            if ln.strip():
                r = json.loads(ln)
                latest[(r.get("check"), r.get("board"))] = r
        if latest:
            return list(latest.values())
    rp = out_root / "results.json"
    return json.loads(rp.read_text()) if rp.exists() else []


def _summary(out_root: Path) -> dict:
    records = _latest_records(out_root)
    boards = sorted({r["board"] for r in records})
    req_ids = sorted({q for r in records for q in r.get("requirements", [])})
    reqs = []
    for q in req_ids:
        recs = [r for r in records if q in r.get("requirements", [])]
        meta = reqmeta.describe(q)
        cells = {bd: {"state": (st := _cell({r["verdict"] for r in recs if r["board"] == bd})),
                      "symbol": SYMBOL[st]} for bd in boards}
        reqs.append({"id": q, "title": meta.get("title", ""), "desc": meta.get("desc", ""),
                     "verify": meta.get("verify", ""), "priority": meta.get("priority"),
                     "cells": cells,
                     "records": [{k: r.get(k) for k in
                                  ("board", "check", "drivers", "actions", "verdict", "severity",
                                   "title", "detail", "evidence")} for r in recs]})
    counts: dict = {}
    for r in records:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    return {"boards": boards,
            "requirements": reqs,
            "suite": next((r.get("suite") for r in records if r.get("suite")), ""),
            "bench": next((r.get("bench") for r in records if r.get("bench")), ""),
            "counts": counts, "symbols": SYMBOL}


def _runs(out_root: Path, limit: int = 1000) -> list:
    p = out_root / "runs.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    return rows[-limit:][::-1]


def _safe(out_root: Path, rel: str) -> Path | None:
    base = out_root.resolve()
    target = (out_root / rel).resolve()
    return target if str(target).startswith(str(base) + "/") and target.is_file() else None


def _git_slug(url: str) -> str:
    """A short `owner/repo` label from a git URL (https / scp-like / ssh, GitHub·GitLab·Gerrit·…)."""
    u = (url or "").strip().rstrip("/")
    if u.endswith(".git"):
        u = u[:-4]
    if "://" in u:                                   # scheme://[user@]host[:port]/path…
        rest = u.split("://", 1)[1]
        u = rest.split("/", 1)[1] if "/" in rest else rest
    elif ":" in u and "/" in u.split(":", 1)[1]:     # scp-like git@host:owner/repo
        u = u.split(":", 1)[1]
    parts = [p for p in u.split("/") if p]
    return "/".join(parts[-2:]) if parts else (url or "")


def _framework_version() -> dict:
    """The running framework's own git revision — `{commit, ref, dirty}` (best-effort; empty commit
    when not a git checkout, e.g. a pip install)."""
    from atf.core.checks import _git
    root = Path(__file__).resolve().parents[2]        # atf/web/server.py → repo root
    commit = _git(root, ["rev-parse", "--short", "HEAD"])
    ref = _git(root, ["rev-parse", "--abbrev-ref", "HEAD"]) if commit else ""
    dirty = bool(_git(root, ["status", "--porcelain"])) if commit else False
    return {"commit": commit, "ref": "" if ref == "HEAD" else ref, "dirty": dirty}


# ------------------------- run engine -------------------------
class RunState:
    def __init__(self, out_root: Path, bench_path: str, repo=None, locks=None):
        self.out_root = out_root
        self.bench_path = bench_path
        self.repo = repo
        self.locks = locks
        self.lock = threading.Lock()
        self.events: list = []
        self.active = False
        self.pending = None
        self.plan: list = []            # the check ids of the run in progress
        self.who = ""                   # a label of the run in progress (suite/bench)
        self.cancel_event = threading.Event()
        self.hub = None                 # set by build_app; enables agent fallback for runs

    def _emit(self, ev: dict):
        self.events.append(ev)

    def events_since(self, n: int) -> list:
        return self.events[n:]

    def start(self, params: dict) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        self.events = []
        self.pending = None
        self.plan = []
        self.who = params.get("suite") or "ad-hoc"
        self.cancel_event.clear()
        self.active = True
        threading.Thread(target=self._run, args=(params,), daemon=True).start()
        return True

    def status(self) -> dict:
        """What (if anything) is running right now — for the UI's run indicator."""
        p = self.pending
        return {"active": self.active, "who": self.who, "plan": self.plan,
                "cancelling": self.cancel_event.is_set(),
                "waiting_on": ({"check": p["check_id"], "instructions": p["instructions"]}
                               if p else None)}

    def request_cancel(self) -> bool:
        """Signal the running job to stop: unblock any pending manual prompt, and set the cancel
        flag the runner checks between checks. Returns False if nothing is running."""
        if not self.active:
            return False
        self.cancel_event.set()
        p = self.pending
        if p and p.get("ev"):                       # unblock a check waiting on operator input
            p["answer"] = {"observation": "run cancelled", "verdict": "skipped"}
            p["ev"].set()
        self._emit({"type": "cancelling"})
        return True

    def _load_bench(self, name: str):
        from atf.core import inventory
        if name and self.repo:
            try:
                if self.repo.get_bench(name) is not None:
                    return self.repo.inventory_bench(name)
            except Exception:
                pass
        return inventory.load(self.bench_path)

    def _run(self, params: dict):
        import time as _time

        from atf.core import manual, report, runner
        from atf.core import suite as suitemod
        from atf.core.registry import resolve_selection, select
        from atf.web.locks import touched_resources
        run_id = None
        lock_held = False
        try:
            self._emit({"type": "start", "params": params})
            bench_name = params.get("bench") or ""
            raw = None
            if params.get("suite") and not (params.get("req") or params.get("ids")):
                # Resolve a saved suite (model / req / include / exclude) — same logic as the agent worker.
                raw = (self.repo.get_suite(params["suite"]) if self.repo and
                       self.repo.get_suite(params["suite"]) else suitemod.load(params["suite"]))
                specs = resolve_selection(raw.get("select", raw) or {})
            else:
                # ad-hoc run: explicit selectors from the request
                specs = select(requirements=params.get("req"),
                               drivers=params.get("vector"),
                               ids=params.get("ids"))
            # No server-side checks match? The checks may come from a connected agent (no repo
            # configured) — run the suite through that agent's uploaded working tree instead.
            via_agent, sel_for_agent = None, None
            if not specs:
                sel_for_agent = (raw.get("select", raw) if raw else
                                 {"req": params.get("req"), "ids": params.get("ids"),
                                  "vectors": params.get("vector")})
                via_agent = self._pick_agent(sel_for_agent)
                if via_agent is None:
                    self._emit({"type": "done", "counts": {},
                                "note": "no checks selected — the server has no checks and no connected agent provides them"})
                    return
            bench = self._load_bench(bench_name)
            boards = set(params["board"]) if params.get("board") else None
            bench_stem = bench_name or Path(self.bench_path).stem
            # one identity for this run: names the lock, the records, the report, and the per-run
            # artifact dir (so evidence/report survive the next run instead of being overwritten)
            run_id = f"run-{int(_time.time() * 1000)}"
            run_dir = self.out_root / "runs" / run_id
            if self.locks is not None:                  # per-resource lock (reject on conflict)
                who = f"agent:{via_agent.name}" if via_agent else f"pilot:{params.get('suite') or 'ad-hoc'}"
                ok, conflict = self.locks.acquire(touched_resources(bench, boards), run_id, who=who)
                if not ok:
                    self._emit({"type": "error", "message": "bench busy — " + "; ".join(
                        f"{r} in use by {h['who']}" for r, h in conflict.items())})
                    run_id = None
                    return
                lock_held = True
            manual.set_prompter(self._web_prompter)

            def on_record(r):
                self._emit({"type": "record", "check": r.check, "board": r.board,
                            "drivers": r.drivers, "actions": r.actions, "verdict": r.verdict,
                            "severity": r.severity, "title": r.title})

            run_meta: dict = {}
            if via_agent is not None:
                self._emit({"type": "plan", "checks": [], "note": f"running via agent {via_agent.name}"})
                import shutil
                from atf.core.runner import Record
                out_dir = None
                try:
                    raw_recs, _dev, out_dir, run_meta = _agent_worker_run(
                        self.hub, via_agent, bench_stem, list(boards) if boards else None,
                        sel_for_agent, params.get("mgmt_backend", "local"))
                    # persist the agent's out dir (evidence + run-meta) as this run's artifact dir
                    run_dir.parent.mkdir(parents=True, exist_ok=True)
                    if run_dir.exists():
                        shutil.rmtree(run_dir, ignore_errors=True)
                    shutil.move(str(out_dir), str(run_dir))
                    out_dir = None
                    run_meta["run_id"] = run_id
                    (run_dir / "run-meta.json").write_text(json.dumps(run_meta, ensure_ascii=False))
                    recs = []
                    for d in raw_recs:
                        d.pop("evidence_text", None)
                        d["suite"] = params.get("suite", "") or d.get("suite", "")
                        d["bench"] = bench_stem
                        d["run_id"] = run_id
                        rec = Record(**d)
                        recs.append(rec)
                        on_record(rec)
                finally:
                    if out_dir:
                        shutil.rmtree(out_dir, ignore_errors=True)
            else:
                self.plan = [s.id for s in specs]
                self._emit({"type": "plan", "checks": self.plan})
                recs = runner.run(bench, specs, run_dir, boards_filter=boards,
                                  mgmt_backend=params.get("mgmt_backend", "docker"),
                                  suite=params.get("suite", ""),
                                  bench_name=bench_stem, on_record=on_record,
                                  cancel=self.cancel_event, run_id=run_id,
                                  suite_select=(raw.get("select", raw) if raw else None))
            counts = report.write(recs, run_dir, history_root=self.out_root,
                                  select=(raw.get("select", raw) if raw else None))
            if not run_meta:                       # in-process path: the runner wrote it into run_dir
                try:
                    run_meta = json.loads((run_dir / "run-meta.json").read_text())
                except Exception:
                    run_meta = {}
            # record the report (owned by the user who ran it) so it shows in VIEW › Reports
            if self.repo and recs:
                try:
                    self.repo.add_report(run_id=run_id, owner=params.get("owner", ""),
                                         suite=params.get("suite") or "ad-hoc", bench=bench_stem,
                                         board=",".join(params.get("board") or []), counts=counts,
                                         select=(raw.get("select", raw) if raw else {}),   # map snapshot
                                         meta=run_meta)                                     # bench + versions
                except Exception:
                    pass
            self._emit({"type": "done", "counts": counts, "run_id": run_id})
        except Exception as e:
            self._emit({"type": "error", "message": f"{type(e).__name__}: {e}"})
        finally:
            from atf.core import manual
            manual.set_prompter(None)
            if self.locks is not None and lock_held and run_id:
                self.locks.release(run_id)
            self.plan = []
            self.active = False
            self.lock.release()

    def _pick_agent(self, sel):
        """Choose a connected agent to run a selection the server registry can't cover. Prefers the
        agent whose advertised catalog covers the most of the requirements/check-ids requested."""
        hub = getattr(self, "hub", None)
        agents = hub.alive() if hub else []
        if not agents:
            return None
        sel = sel or {}
        rlist = sel.get("requirements")
        if isinstance(rlist, list) and rlist and isinstance(rlist[0], dict):   # new Suite-as-map shape
            reqs = {r.get("id") for r in rlist}
            ids = {t.get("id") for r in rlist for t in (r.get("tests") or [])}
        else:                                                                  # legacy {req, include, ids}
            reqs = set(sel.get("req") or (rlist if isinstance(rlist, list) else []) or [])
            ids = set(sel.get("include") or []) | set(sel.get("ids") or [])
        def score(a):
            n = 0
            for c in (a.catalog or []):
                if c.get("id") in ids:
                    n += 1
                if reqs and set(c.get("requirements") or []) & reqs:
                    n += 1
            return n
        return max(agents, key=score)

    def _web_prompter(self, instructions, default_severity, check_id=""):
        from atf.core import manual
        ev = threading.Event()
        self.pending = {"check_id": check_id, "instructions": instructions,
                        "ev": ev, "answer": None}
        self._emit({"type": "manual", "check": check_id, "instructions": instructions})
        ev.wait(timeout=1800)
        ans = (self.pending or {}).get("answer") or {}
        self.pending = None
        verdict = manual._verdict(ans.get("verdict", ""))
        self._emit({"type": "manual-done", "check": check_id, "verdict": verdict.value})
        return manual.result_from(ans.get("observation", ""), verdict, default_severity)

    def answer(self, observation: str, verdict: str) -> bool:
        p = self.pending
        if not p:
            return False
        p["answer"] = {"observation": observation, "verdict": verdict}
        p["ev"].set()
        return True


def _meta(bench_path: str, repo) -> dict:
    from atf.core import inventory
    from atf.core import suite as suitemod
    from atf.core.registry import REGISTRY
    benches = repo.list_benches() if repo else []
    default = Path(bench_path).stem
    boards = []
    if repo and any(b["name"] == default for b in benches):
        boards = [bd["name"] for bd in (repo.get_bench(default) or {}).get("boards", [])]
    if not boards:
        try:
            boards = [b.name for b in inventory.load(bench_path).boards]
        except Exception:
            boards = []
    suites = sorted({s["name"] for s in (repo.list_suites() if repo else [])}
                    | {n for n, _ in suitemod.available()})
    # label each source root (repo name for a synced checkout, else the dir name) → which repo a check came from
    import sys
    from atf.core.checks import source_paths
    # a synced Repository checkout carries its git identity (owner/repo); a bare $ATF_CHECK_SOURCES
    # path is a local source. roots: (resolved_path, label, kind, git_slug).
    ck = {str(Path(s["checkout"]).resolve()): s for s in (repo.list_check_sources() if repo else [])
          if s.get("last_status") == "ok" and s.get("checkout")}
    roots = []
    for p in source_paths():
        rp = str(Path(p).resolve())
        src = ck.get(rp)
        if src and src.get("kind") == "path":
            roots.append((rp, src["name"], "path", Path(src.get("url") or rp).name))
        elif src:
            roots.append((rp, src["name"], "git", _git_slug(src.get("url", ""))))
        else:
            roots.append((rp, Path(p).name, "local", ""))

    def _repo_of(spec):
        # a Markdown manual test carries its source file directly; a .py check maps via its module
        f = getattr(spec, "path", "") or getattr(sys.modules.get(getattr(spec.fn, "__module__", "")), "__file__", None)
        if f:
            rf = str(Path(f).resolve())
            for root, label, kind, slug in roots:
                if rf.startswith(root + "/"):
                    return {"source": label, "source_kind": kind, "git": slug}
        return {"source": "", "source_kind": "local", "git": ""}
    checks = [{"id": s.id, "drivers": sorted(s.drivers), "actions": sorted(s.actions),
               "mode": s.mode, "disruptive": s.disruptive, "requirements": list(s.requirements),
               "model": s.model, "title": s.title, "path": getattr(s, "path", ""),
               **_repo_of(s)} for s in REGISTRY.values()]
    sources = repo.list_check_sources() if repo else []
    return {"suites": suites, "boards": boards, "checks": checks, "benches": benches,
            "sources": sources}


def _seed_from_files(repo, roots=None) -> None:
    """First run: populate each store table from YAML found under one or more roots, so the
    dashboard isn't empty. Roots = the working dir plus every check-source repo (which is where
    benches/suites/requirements now live post repo-split). Seeds per-table (empty check each)."""
    if roots is None:
        from atf.core.checks import source_paths
        roots = [Path(".")] + source_paths()
    roots = [Path(r) for r in roots]

    def _yamls(sub):
        for root in roots:
            d = root / sub
            if d.is_dir():
                yield from sorted(d.glob("*.yaml"))

    if not repo.list_benches():
        for f in _yamls("benches"):
            if f.name.endswith(".secrets.yaml") or f.name.endswith(".secrets.example.yaml"):
                continue
            try:
                repo.upsert_bench(f.stem, yaml.safe_load(f.read_text()) or {})
                sec = f.with_name(f.stem + ".secrets.yaml")
                if sec.exists():
                    repo.set_secrets(f.stem, yaml.safe_load(sec.read_text()) or {})
            except Exception:
                pass
    if not repo.list_suites():
        for f in _yamls("suites"):
            try:
                repo.upsert_suite(f.stem, yaml.safe_load(f.read_text()) or {})
            except Exception:
                pass
    # requirements are NOT seeded here — they are loaded (and cleaned) by _reload_requirements
    # from the configured sources only, so they never persist from an implicit/dev path.


def _seed_inventory(repo) -> None:
    """First run: populate the shared inventory from existing benches' agents/boards, so it isn't
    empty and benches have resources to reference (Phase A → B bridge)."""
    if repo.list_inv_agents() or repo.list_inv_boards():
        return
    for bn in [b["name"] for b in repo.list_benches()]:
        bench = repo.get_bench(bn) or {}
        for an, a in (bench.get("agents") or {}).items():
            ssh = a.get("ssh") or {}
            repo.upsert_inv_agent(an, a.get("platform", "linux"), a.get("host", ""),
                                  ssh.get("user", ""), ssh.get("password_ref", ""))
        for b in (bench.get("boards") or []):
            creds = [{"role": r, "user": (c or {}).get("user", ""), "ref": (c or {}).get("password_ref", "")}
                     for r, c in (b.get("creds") or {}).items()]
            repo.upsert_inv_board(b["name"], {"model": b.get("model", ""), "serial": b.get("serial", ""),
                                              "mgmt": b.get("mgmt") or {}, "creds": creds})


def _reload_requirements(repo) -> int:
    """The DB requirements = exactly what the configured sources currently provide. PURGE any
    catalog no longer backed by a live source (cleans stale/previously-synced leftovers), then
    (re)import each source's requirements/*.yaml. With no configured source → 0 requirements;
    agent-provided catalogs are never persisted (overlay only)."""
    from atf.core.checks import source_paths
    live = set(_framework_sources(repo).keys())
    for f in repo.frameworks():
        if f["framework"] not in live:
            repo.delete_framework(f["framework"])       # drop leftovers (e.g. an old sync / dev seed)
    n = 0
    for p in source_paths():
        d = Path(p) / "requirements"
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            try:
                fw, title, parsed = reqmeta.parse_yaml(f.read_text())
                repo.import_requirements(fw or f.stem, parsed, title=title)
                n += 1
            except Exception:
                pass
    reqmeta.invalidate()
    return n


def _framework_sources(repo) -> dict:
    """Map each requirement framework → the live source(s) currently providing it (the repo name
    for a synced check_source checkout, else the source dir name for dev siblings). A framework
    here is 'synced' (backed by a live source); absent = a cached snapshot whose source is gone."""
    from atf.core.checks import source_paths
    label_of = {str(Path(s["checkout"]).resolve()): s["name"]
                for s in repo.list_check_sources()
                if s.get("last_status") == "ok" and s.get("checkout")}
    out: dict = {}
    for p in source_paths():
        rp = Path(p).resolve()
        label = label_of.get(str(rp), rp.name)          # repo name if a synced checkout, else dir name
        d = rp / "requirements"
        if not d.is_dir():
            continue
        for f in d.glob("*.yaml"):
            try:
                fw, _, _ = reqmeta.parse_yaml(f.read_text())
                out.setdefault(fw or f.stem, set()).add(label)
            except Exception:
                pass
    return {k: sorted(v) for k, v in out.items()}


def _repo_provides(checkout: str | None) -> dict:
    """What a synced checkout contributes: check count, model namespaces, requirement catalogs."""
    p = Path(checkout) if checkout else None
    if not p or not p.is_dir():
        return {}
    cdir = p / "atf_checks"
    # automated (.py, excluding package __init__) + Markdown manual tests (.md)
    files = [f for f in cdir.rglob("*.py")
             if "__pycache__" not in f.parts and f.name != "__init__.py"] if cdir.is_dir() else []
    md = [f for f in cdir.rglob("*.md") if "__pycache__" not in f.parts] if cdir.is_dir() else []
    models = sorted({f.relative_to(cdir).parts[0] for f in files + md}) if (files or md) else []
    cats = sorted(f.stem for f in (p / "requirements").glob("*.yaml")) if (p / "requirements").is_dir() else []
    return {"checks": len(files) + len(md), "models": models, "catalogs": cats}


def _exec_last_expr(src: str, g: dict):
    """exec a block; if its last statement is an expression, eval it and return repr(value)
    (notebook-style echo). Returns None when the block ends in a statement."""
    import ast
    tree = ast.parse(src)
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = ast.Expression(tree.body.pop().value)
    exec(compile(tree, "<scratch>", "exec"), g)     # noqa: S102 - dev workbench, admin-gated
    if last is not None:
        return repr(eval(compile(last, "<scratch>", "eval"), g))   # noqa: S307
    return None


# ------------------------- FastAPI app -------------------------
def _agent_worker_run(hub, s, bench_stem: str, board, select: dict, mgmt_backend: str, timeout: int = 900):
    """Upload a connected agent's working tree and run `atf _agent-worker` against it (the agent's
    checks execute on the server from its uploaded sources). Returns (records, dev_dirs, out_dir, meta)
    where meta is the run-meta snapshot (bench + the agent trees' versions as loaded). Caller owns
    out_dir cleanup. `board` may be a str or a list."""
    import shutil
    import subprocess as sp
    import sys
    import tempfile

    from atf.core.checks import source_paths
    tok, ev = hub.request_tree(s)
    if not ev.wait(timeout=45):
        raise RuntimeError("agent did not upload its tree in time")
    tree = s.uploads[tok]["dir"]
    dev_dirs = [str(p) for p in sorted(Path(tree).iterdir()) if p.is_dir()]   # dev trees win
    srcs = dev_dirs + [str(p) for p in source_paths()]
    out = Path(tempfile.mkdtemp(prefix="atf-agent-run-"))
    req = {"bench": bench_stem, "board": board, "out": str(out),
           "select": select, "mgmt_backend": mgmt_backend}
    env = {**os.environ, "ATF_CHECK_SOURCES": os.pathsep.join(srcs)}
    try:
        p = sp.run([sys.executable, "-m", "atf.cli", "_agent-worker"],
                   input=json.dumps(req), env=env, capture_output=True, text=True, timeout=timeout)
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    if p.returncode != 0:
        shutil.rmtree(out, ignore_errors=True)
        raise RuntimeError(f"agent-worker failed: {p.stderr[-800:]}")
    # results come back via a file (robust against stdout noise from a check's prints)
    rec_file = out / "records.json"
    if not rec_file.is_file():
        shutil.rmtree(out, ignore_errors=True)
        raise RuntimeError(f"agent-worker produced no records: {(p.stderr or p.stdout)[-800:]}")
    meta = {}
    mp = out / "run-meta.json"
    if mp.is_file():
        try:
            meta = json.loads(mp.read_text())
        except Exception:
            meta = {}
    return json.loads(rec_file.read_text()), dev_dirs, out, meta


def build_app(out_root: Path, bench_path: str, repo) -> FastAPI:
    import secrets as pysecrets
    app = FastAPI(title="atf — Anytest Framework", docs_url="/api/docs",
                  redoc_url="/api/redoc", openapi_url="/api/openapi.json")
    locks = ResourceLocks()
    state = RunState(out_root, bench_path, repo, locks)
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
    tokens: dict = {}                   # token -> {"username", "is_admin"}
    hub = AgentHub()
    state.hub = hub                     # let a pilot run fall back to a connected agent's checks

    @app.middleware("http")
    async def _login_gate(request: Request, call_next):
        """The whole app requires login. /api/* needs a valid user token, EXCEPT: login itself,
        whoami (self-checks), the agent-facing endpoints (agent authenticates with its enrollment
        token, not a user login), and the SSE stream (EventSource can't send an Authorization header)."""
        p, meth = request.url.path, request.method
        if p.startswith("/api/"):
            openep = p in ("/api/admin/login", "/api/admin/whoami", "/api/agents/register", "/api/run/stream",
                           "/api/version", "/api/docs", "/api/redoc", "/api/openapi.json")
            if not openep and p.startswith("/api/agents/"):
                tail = p.rsplit("/", 1)[-1]
                openep = (meth == "GET" and tail == "poll") or \
                         (meth == "POST" and tail in ("tree", "inspect", "catalog", "file"))
            if not openep:
                tok = request.headers.get("authorization", "").replace("Bearer ", "").strip()
                if tok not in tokens:
                    return JSONResponse({"detail": "authentication required"}, status_code=401)
        return await call_next(request)

    def _user_of(authorization: str):
        return tokens.get(authorization.replace("Bearer ", "").strip())

    def require_login(authorization: str = Header(default="")):
        u = _user_of(authorization)
        if not u:
            raise HTTPException(401, "authentication required")
        return u

    def require_admin(authorization: str = Header(default="")):
        u = _user_of(authorization)
        if not u or not u.get("is_admin"):
            raise HTTPException(401, "admin authentication required")
        return u

    @app.post("/api/admin/login")
    def admin_login(body: dict = Body(...)):
        u = repo.verify_user(body.get("user", ""), body.get("password", ""))
        if u:
            tok = pysecrets.token_hex(16)
            tokens[tok] = u
            return {"ok": True, "token": tok, "user": u["username"], "is_admin": u["is_admin"]}
        raise HTTPException(401, "invalid credentials")

    @app.get("/api/admin/whoami")
    def admin_whoami(authorization: str = Header(default="")):
        u = _user_of(authorization)
        if not u:
            raise HTTPException(401, "authentication required")
        return {"ok": True, "user": u["username"], "is_admin": u["is_admin"]}

    @app.post("/api/admin/password")                    # change YOUR OWN password
    def admin_password(body: dict = Body(...), me=Depends(require_login)):
        if not body.get("password"):
            raise HTTPException(400, "need password")
        repo.set_user_password(me["username"], body["password"])
        return {"ok": True}

    # ---- user management (admin only) ----
    @app.get("/api/users")
    def users_list(_=Depends(require_admin)):
        return repo.list_users()

    @app.post("/api/users")                             # create or update a user (password optional on update)
    def users_upsert(body: dict = Body(...), _=Depends(require_admin)):
        un = (body.get("username") or "").strip()
        if not un:
            raise HTTPException(400, "need username")
        if not repo.list_users() or not any(u["username"] == un for u in repo.list_users()):
            if not body.get("password"):
                raise HTTPException(400, "new user needs a password")
        # never leave the store with zero admins
        if any(u["username"] == un and u["is_admin"] for u in repo.list_users()) \
                and not body.get("is_admin") and repo.admin_count() <= 1:
            raise HTTPException(400, "can't remove the last admin")
        repo.upsert_user(un, bool(body.get("is_admin")), body.get("password") or None)
        return {"ok": True}

    @app.post("/api/users/{username}/password")
    def users_password(username: str, body: dict = Body(...), _=Depends(require_admin)):
        if not body.get("password"):
            raise HTTPException(400, "need password")
        repo.set_user_password(username, body["password"])
        return {"ok": True}

    @app.delete("/api/users/{username}")
    def users_delete(username: str, me=Depends(require_admin)):
        if username == me["username"]:
            raise HTTPException(400, "you can't delete yourself")
        tgt = next((u for u in repo.list_users() if u["username"] == username), None)
        if tgt and tgt["is_admin"] and repo.admin_count() <= 1:
            raise HTTPException(400, "can't delete the last admin")
        repo.delete_user(username)
        return {"ok": True}

    @app.get("/api/board-models")
    def board_models():
        return repo.list_board_models()

    @app.post("/api/board-models")
    def board_model_upsert(body: dict = Body(...), _=Depends(require_admin)):
        if not body.get("name"):
            raise HTTPException(400, "need name")
        repo.upsert_board_model(body["name"], body.get("description", ""), body.get("slug", ""))
        return {"ok": True}

    @app.delete("/api/board-models/{name}")
    def board_model_delete(name: str, _=Depends(require_admin)):
        repo.delete_board_model(name)
        return {"ok": True}

    # ---- upstream check repositories (server tries to sync; falls back to agents on failure) ----
    @app.get("/api/check-sources")
    def check_sources(_=Depends(require_admin)):
        out = []
        for s in repo.list_check_sources():
            s = dict(s)
            s["provides"] = _repo_provides(s.get("checkout"))
            out.append(s)
        return out

    @app.post("/api/check-sources")
    def check_source_upsert(body: dict = Body(...), _=Depends(require_admin)):
        if not body.get("name") or not body.get("url"):
            raise HTTPException(400, "need name + url")
        # token: a non-empty value sets it (encrypted); clear_token clears it; otherwise it's
        # left unchanged (so editing a repo doesn't require re-entering the token).
        token = "" if body.get("clear_token") else (body.get("token") or None)
        repo.upsert_check_source(body["name"], body["url"], body.get("ref", "main"),
                                 body.get("enabled", True), token=token,
                                 kind=body.get("kind", "git"))
        return {"ok": True}

    @app.delete("/api/check-sources/{name}")
    def check_source_delete(name: str, _=Depends(require_admin)):
        import shutil
        from atf.core.checks import reload_upstream
        info = repo.clear_check_source_sync(name) or {}     # drop a server-cloned dir (never a path source's)
        if info.get("kind") == "git" and info.get("checkout"):
            shutil.rmtree(info["checkout"], ignore_errors=True)
        repo.delete_check_source(name)
        reload_upstream()                                   # its tests/requirements stop loading now
        _reload_requirements(repo)
        return {"ok": True}

    @app.post("/api/check-sources/sync")
    def check_sources_sync(me=Depends(require_admin)):
        from atf.core.checks import reload_upstream, sync_sources
        from atf.core.registry import REGISTRY
        results = sync_sources(repo, by=me.get("user", ""))
        reload_upstream()                               # hot-swap checks (code + Markdown manual tests)
        n_reqs = _reload_requirements(repo)            # a repo provides Requirements too
        return {"results": results, "upstream_checks": len(REGISTRY), "requirement_catalogs": n_reqs}

    @app.post("/api/check-sources/{name}/unsync")
    def check_source_unsync(name: str, _=Depends(require_admin)):
        """Drop a repo's local clone so its tests/requirements stop loading (the repo config stays —
        Sync re-clones it). Does not touch $ATF_CHECK_SOURCES sources (those aren't server-managed)."""
        import shutil
        from atf.core.checks import reload_upstream
        from atf.core.registry import REGISTRY
        info = repo.clear_check_source_sync(name) or {}
        # only delete a dir the SERVER cloned (kind=git); a `path` source points at the user's own
        # directory — never remove that, just unlink it
        if info.get("kind") == "git" and info.get("checkout"):
            shutil.rmtree(info["checkout"], ignore_errors=True)
        reload_upstream()
        _reload_requirements(repo)
        return {"ok": True, "upstream_checks": len(REGISTRY)}

    # ---- config-store backup / restore (server migration) ----
    @app.get("/api/admin/backup")
    def admin_backup(_=Depends(require_admin)):
        """Download a consistent snapshot of the whole config store (a SQLite file)."""
        import shutil
        import tempfile
        from datetime import datetime

        from starlette.background import BackgroundTask
        tmpdir = Path(tempfile.mkdtemp(prefix="atf-backup-"))
        repo.backup(tmpdir / "store.db")
        fn = f"atf-store-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        return FileResponse(str(tmpdir / "store.db"), filename=fn,
                            media_type="application/octet-stream",
                            background=BackgroundTask(lambda: shutil.rmtree(tmpdir, ignore_errors=True)))

    @app.post("/api/admin/restore")
    async def admin_restore(request: Request, _=Depends(require_admin)):
        """Replace the config store from an uploaded snapshot (raw body). Disruptive: overwrites
        every bench/suite/secret/user. Secrets decrypt only under the same APP_SECRET."""
        import shutil
        import tempfile
        data = await request.body()
        if not data:
            raise HTTPException(400, "empty upload — POST the backup file as the request body")
        tmpdir = Path(tempfile.mkdtemp(prefix="atf-restore-"))
        try:
            (tmpdir / "store.db").write_bytes(data)
            repo.restore(tmpdir / "store.db")
        except (ValueError, FileNotFoundError) as e:
            raise HTTPException(400, str(e))
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        from atf.core.checks import reload_upstream         # config changed wholesale → reload
        reload_upstream()
        _reload_requirements(repo)
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(_STATIC / "index.html")

    # ---- read views ----
    @app.get("/api/summary")
    def summary():
        return _summary(out_root)

    @app.get("/api/runs")
    def runs():
        return _runs(out_root)

    # ---- reports (VIEW › Reports): one per Test Plan execution, owned + private/public ----
    def _records_of(run_id: str) -> list:
        p = out_root / "runs.jsonl"
        if not p.exists():
            return []
        out = []
        for ln in p.read_text().splitlines():
            if ln.strip():
                r = json.loads(ln)
                if r.get("run_id") == run_id:
                    out.append(r)
        return out

    @app.get("/api/reports")
    def reports_list(me=Depends(require_login)):
        rows = repo.list_reports(me["username"], me["is_admin"])
        for r in rows:
            r["mine"] = (r["owner"] == me["username"])
        return rows

    @app.get("/api/reports/{run_id}")
    def report_get(run_id: str, me=Depends(require_login)):
        from atf.core import requirements as reqmeta
        from atf.core.registry import requirement_verdicts
        rep = repo.get_report(run_id, me["username"], me["is_admin"])
        if rep is None:
            raise HTTPException(404, "report not found (or private)")
        rep["mine"] = (rep["owner"] == me["username"])
        recs = _records_of(run_id)
        rv = requirement_verdicts(rep.get("select"), recs)   # {} for legacy suites
        for q in (rv.get("requirements") or []):             # enrich the roll-up with catalog text + version
            m = reqmeta.describe(q["id"])
            q["title"] = m.get("title", "")
            q["version"] = reqmeta.requirement_sha(m) if m else ""
        meta = rep.get("meta") or {}
        ver = {s["name"]: s for s in (meta.get("sources") or [])}   # source repo → version, as loaded
        for r in recs:                                             # attach each test's source version
            sv = ver.get(r.get("source", ""))
            r["source_version"] = (sv.get("commit", "") + (" · dirty" if sv.get("dirty") else "")) if sv else ""
        return {"report": rep, "records": recs, "requirements": rv,
                "bench_boards": (meta.get("bench") or {}).get("boards") or [],
                "sources": meta.get("sources") or []}

    @app.post("/api/reports/{run_id}/visibility")
    def report_visibility(run_id: str, body: dict = Body(...), me=Depends(require_login)):
        if not repo.set_report_visibility(run_id, body.get("visibility", "private"),
                                          me["username"], me["is_admin"]):
            raise HTTPException(403, "not your report")
        return {"ok": True}

    @app.delete("/api/reports/{run_id}")
    def report_delete(run_id: str, me=Depends(require_login)):
        if not repo.delete_report(run_id, me["username"], me["is_admin"]):
            raise HTTPException(403, "not your report")
        return {"ok": True}

    def _run_dir(run_id: str) -> Path:
        return out_root / "runs" / run_id

    def _report_ctx(rep: dict, recs: list) -> dict:
        """Rebuild the report render context from a stored report row + its records (fallback when the
        persisted report.md/html isn't on disk — e.g. a report restored onto a fresh server)."""
        from atf.core import report as reportmod
        from atf.core.runner import Record
        fields = set(Record.__dataclass_fields__)
        objs = [Record(**{k: v for k, v in d.items() if k in fields}) for d in recs]
        meta = dict(rep.get("meta") or {})
        meta.setdefault("select", rep.get("select") or {})
        meta.setdefault("suite", rep.get("suite") or "")
        meta["evidence_root"] = str(_run_dir(rep["run_id"]))
        return reportmod.context_from_records(objs, meta)

    @app.get("/api/reports/{run_id}/export")
    def report_export(run_id: str, format: str = "json", me=Depends(require_login)):
        from atf.core import report as reportmod
        rep = repo.get_report(run_id, me["username"], me["is_admin"])
        if rep is None:
            raise HTTPException(404, "report not found (or private)")
        recs = _records_of(run_id)
        stem = f"report-{rep['suite']}-{run_id}".replace("/", "_").replace(" ", "_")
        rd = _run_dir(run_id)
        if format in ("md", "html"):
            f = rd / f"report.{format}"                          # prefer the persisted artifact
            text = f.read_text() if f.is_file() else (
                reportmod.render_md if format == "md" else reportmod.render_html)(_report_ctx(rep, recs))
            if format == "html":                                 # inline so it opens in a browser tab
                return Response(text, media_type="text/html; charset=utf-8")
            return PlainTextResponse(text, media_type="text/markdown",
                                     headers={"Content-Disposition": f'attachment; filename="{stem}.md"'})
        return PlainTextResponse(json.dumps({"report": rep, "records": recs}, indent=2, ensure_ascii=False),
                                 media_type="application/json",
                                 headers={"Content-Disposition": f'attachment; filename="{stem}.json"'})

    @app.get("/api/reports/{run_id}/download")
    def report_download(run_id: str, me=Depends(require_login)):
        """A results package (.zip): the run's report.md/html, matrix, findings, results.json,
        run-meta and ALL evidence files — everything needed to read the report offline."""
        import io
        import zipfile
        rep = repo.get_report(run_id, me["username"], me["is_admin"])
        if rep is None:
            raise HTTPException(404, "report not found (or private)")
        rd = _run_dir(run_id)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            if rd.is_dir():
                for p in sorted(rd.rglob("*")):
                    if p.is_file():
                        z.write(p, p.relative_to(rd).as_posix())
            # always include the stored row + records, even if the on-disk dir is gone
            z.writestr("report-row.json",
                       json.dumps({"report": rep, "records": _records_of(run_id)}, indent=2, ensure_ascii=False))
        stem = f"report-{rep['suite']}-{run_id}".replace("/", "_").replace(" ", "_")
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{stem}.zip"'})

    @app.get("/api/version")
    def version():
        return _framework_version()

    @app.get("/api/meta")
    def meta():
        return _meta(bench_path, repo)

    @app.get("/api/findings")
    def findings():
        d = out_root / "findings"
        return sorted(p.name for p in d.glob("*.md")) if d.is_dir() else []

    @app.get("/api/finding")
    def finding(name: str):
        f = _safe(out_root, f"findings/{name}")
        if not f:
            raise HTTPException(404, "not found")
        return PlainTextResponse(f.read_text(), media_type="text/markdown")

    @app.get("/api/evidence")
    def evidence(path: str, run_id: str = ""):
        # a run's own dir first; fall back to the shared out_root (pre-per-run reports)
        f = None
        if run_id:
            f = _safe(out_root / "runs" / run_id, path)
        f = f or _safe(out_root, path)
        if not f:
            raise HTTPException(404, "not found")
        return PlainTextResponse(f.read_text(errors="replace"))

    # ---- pilot ----
    @app.get("/api/run/state")
    def run_state():
        p = state.pending
        return {"active": state.active,
                "pending": {"check": p["check_id"], "instructions": p["instructions"]} if p else None}

    @app.get("/api/run/status")
    def run_status():
        """What's running now (active, the plan, and what it's waiting on) — for the run indicator."""
        return state.status()

    @app.post("/api/run")
    def run_start(params: dict = Body(default={}), me=Depends(require_login)):
        params["owner"] = me["username"]                # the run (report) is attributed to you
        ok = state.start(params)
        return {"started": ok, "error": None if ok else "a run is already active"}

    @app.post("/api/run/cancel")
    def run_cancel(_=Depends(require_login)):
        """Stop the active run: unblock a pending manual prompt and signal the runner to stop."""
        return {"cancelled": state.request_cancel()}

    @app.post("/api/manual")
    def manual_answer(body: dict = Body(default={})):
        return {"accepted": state.answer(body.get("observation", ""), body.get("verdict", ""))}

    @app.get("/api/run/stream")
    async def run_stream():
        async def gen():
            cursor, ticks = 0, 0
            while ticks < 6000:                       # ~30 min safety cap
                for ev in state.events_since(cursor):
                    yield f"data: {json.dumps(ev)}\n\n".encode()
                    cursor += 1
                    if ev.get("type") in ("done", "error"):
                        return
                await asyncio.sleep(0.3)
                ticks += 1
        return StreamingResponse(gen(), media_type="text/event-stream")

    # ---- config: benches ----
    # ---- shared inventory (public): agents + boards + driver/action entities that benches import ----
    @app.get("/api/actions/catalog")
    def actions_catalog():
        return repo.list_inv_actions()          # node-action entities (built-in power-cycle + user-made)

    # driver TYPES = name + prop schema; `serial`/`ip` are built-in (they carry the channels). A bench
    # instantiates a type on a board with an alias (ctx key) + values for the props.
    @app.get("/api/inventory/drivers")
    def inv_drivers():
        return repo.list_inv_drivers()

    @app.post("/api/inventory/drivers")
    def inv_driver_upsert(body: dict = Body(...), me=Depends(require_login)):
        if not body.get("name"):
            raise HTTPException(400, "need name")
        repo.upsert_inv_driver(body["name"], body.get("description", ""), body.get("props") or [],
                               editor=me["username"])
        return {"ok": True}

    @app.delete("/api/inventory/drivers/{name}")
    def inv_driver_delete(name: str, _=Depends(require_login)):
        repo.delete_inv_driver(name)
        return {"ok": True}

    # node-action entities (name + signal list)
    @app.get("/api/inventory/actions")
    def inv_actions():
        return repo.list_inv_actions()

    @app.post("/api/inventory/actions")
    def inv_action_upsert(body: dict = Body(...), me=Depends(require_login)):
        if not body.get("name"):
            raise HTTPException(400, "need name")
        repo.upsert_inv_action(body["name"], body.get("description", ""), body.get("signals") or [],
                               editor=me["username"])
        return {"ok": True}

    @app.delete("/api/inventory/actions/{name}")
    def inv_action_delete(name: str, _=Depends(require_login)):
        repo.delete_inv_action(name)
        return {"ok": True}

    @app.get("/api/inventory/agents")
    def inv_agents():
        return repo.list_inv_agents()

    @app.post("/api/inventory/agents")
    def inv_agent_upsert(body: dict = Body(...), me=Depends(require_login)):
        if not body.get("name"):
            raise HTTPException(400, "need name")
        repo.upsert_inv_agent(body["name"], body.get("platform", "linux"), body.get("host", ""),
                              body.get("ssh_user", ""), body.get("ssh_secret_ref", ""),
                              editor=me["username"])
        return {"ok": True}

    @app.delete("/api/inventory/agents/{name}")
    def inv_agent_delete(name: str, _=Depends(require_login)):
        repo.delete_inv_agent(name)
        return {"ok": True}

    @app.get("/api/inventory/boards")
    def inv_boards():
        return repo.list_inv_boards()

    @app.post("/api/inventory/boards")
    def inv_board_upsert(body: dict = Body(...), me=Depends(require_login)):
        if not body.get("name"):
            raise HTTPException(400, "need name")
        repo.upsert_inv_board(body["name"], body, editor=me["username"])
        return {"ok": True}

    @app.delete("/api/inventory/boards/{name}")
    def inv_board_delete(name: str, _=Depends(require_login)):
        repo.delete_inv_board(name)
        return {"ok": True}

    @app.post("/api/inventory/ping")                    # reachability probe (ICMP) for a node host / board MGMT IP
    def inv_ping(body: dict = Body(...), _=Depends(require_login)):
        target = (body.get("target") or "").strip()
        if not target:
            raise HTTPException(400, "need target")
        import re as _re
        import subprocess
        if not _re.fullmatch(r"[A-Za-z0-9_.:-]+", target):   # host/ip only — no shell metacharacters
            raise HTTPException(400, "invalid target")
        try:
            r = subprocess.run(["ping", "-c", "1", "-W", "1", target],
                               capture_output=True, text=True, timeout=4,
                               env={**os.environ, "LC_ALL": "C"})   # stable English output to parse
            ok = r.returncode == 0
            m = _re.search(r"time[=<]\s*([\d.]+)\s*ms", r.stdout)
            ms = float(m.group(1)) if m else None
            last = (r.stdout.strip().splitlines() or [""])[-1] if ok else (r.stdout + r.stderr).strip()
            return {"ok": ok, "target": target, "ms": ms, "detail": last[:200]}
        except subprocess.TimeoutExpired:
            return {"ok": False, "target": target, "ms": None, "detail": "timeout"}
        except Exception as e:  # noqa: BLE001 — surface any probe failure as unreachable
            return {"ok": False, "target": target, "ms": None, "detail": str(e)[:200]}

    @app.get("/api/benches")
    def benches():
        return repo.list_benches()

    @app.get("/api/benches/{name}")
    def bench_get(name: str):
        b = repo.get_bench(name)
        if b is None:
            raise HTTPException(404, "bench not found")
        b["secret_refs"] = repo.secret_refs(name)
        return b

    @app.put("/api/benches/{name}")
    def bench_put(name: str, data: dict = Body(...)):
        repo.upsert_bench(name, data)
        return {"ok": True, "name": name}

    @app.delete("/api/benches/{name}")
    def bench_del(name: str):
        repo.delete_bench(name)
        return {"ok": True}

    @app.post("/api/benches/import")
    async def bench_import(req: Request):
        body = await req.json()
        name = body.get("name")
        data = body.get("data") or (yaml.safe_load(body["yaml"]) if body.get("yaml") else None)
        if not name or data is None:
            raise HTTPException(400, "need {name, yaml|data}")
        repo.upsert_bench(name, data)
        if body.get("secrets"):
            repo.set_secrets(name, body["secrets"])
        return {"ok": True, "name": name}

    @app.get("/api/benches/{name}/export")
    def bench_export(name: str):
        b = repo.get_bench(name)
        if b is None:
            raise HTTPException(404, "bench not found")
        return PlainTextResponse(yaml.safe_dump(b, sort_keys=False, allow_unicode=True),
                                 media_type="application/x-yaml",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.yaml"'})

    # ---- config: secrets (values encrypted at rest; never returned in clear) ----
    @app.get("/api/benches/{name}/secrets")
    def secrets_get(name: str, _=Depends(require_login)):
        return {"refs": repo.secret_refs(name), "values": repo.secrets(name)}

    @app.put("/api/benches/{name}/secrets")
    def secrets_put(name: str, body: dict = Body(...), _=Depends(require_login)):
        repo.set_secrets(name, body.get("secrets") or {}, replace=bool(body.get("replace")))
        return {"ok": True, "refs": repo.secret_refs(name)}

    @app.delete("/api/benches/{name}/secrets/{ref}")
    def secret_delete(name: str, ref: str, _=Depends(require_login)):
        repo.delete_secret(name, ref)
        return {"ok": True, "refs": repo.secret_refs(name)}

    # ---- config: suites ----
    @app.get("/api/suites")
    def suites():
        return repo.list_suites()

    @app.get("/api/suites/{name}")
    def suite_get(name: str):
        s = repo.get_suite(name)
        if s is None:
            raise HTTPException(404, "suite not found")
        return s

    @app.put("/api/suites/{name}")
    def suite_put(name: str, data: dict = Body(...)):
        repo.upsert_suite(name, data)
        return {"ok": True, "name": name}

    @app.delete("/api/suites/{name}")
    def suite_del(name: str):
        repo.delete_suite(name)
        return {"ok": True}

    @app.get("/api/suites/{name}/export")
    def suite_export(name: str):
        s = repo.get_suite(name)
        if s is None:
            raise HTTPException(404, "suite not found")
        return PlainTextResponse(yaml.safe_dump(s, sort_keys=False, allow_unicode=True),
                                 media_type="application/x-yaml",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.yaml"'})

    @app.post("/api/suites/validate")                   # every referenced req/test still loaded? sha drifted?
    def suite_validate(body: dict = Body(...), _=Depends(require_login)):
        sel = body.get("select") or {}
        req_sha, test_sha = {}, {}                       # current universe: id -> content sha
        for r in repo.list_requirements():
            req_sha[r["id"]] = r.get("sha", "")
        for c in _meta(bench_path, repo)["checks"]:
            test_sha.setdefault(c["id"], c.get("sha", ""))
        for s in hub.alive():
            for r in _parse_agent_reqs(s.req_files, f"agent:{s.name}"):
                req_sha.setdefault(r["id"], r.get("sha", ""))
            for c in (s.catalog or []):
                if c.get("id"):
                    test_sha[c["id"]] = c.get("sha", "") or test_sha.get(c["id"], "")

        def _chk(kind, idv, stored, universe):
            if idv not in universe:
                return {"kind": kind, "id": idv, "status": "missing", "stored_sha": stored, "current_sha": ""}
            cur = universe.get(idv) or ""
            status = "drift" if (stored and cur and stored != cur) else "ok"
            return {"kind": kind, "id": idv, "status": status, "stored_sha": stored, "current_sha": cur}

        items, reqs = [], sel.get("requirements")
        if isinstance(reqs, list) and reqs and isinstance(reqs[0], dict):
            for rq in reqs:
                items.append(_chk("requirement", rq.get("id"), rq.get("sha1", ""), req_sha))
                for t in (rq.get("tests") or []):
                    items.append(_chk("test", t.get("id"), t.get("sha1", ""), test_sha))
        summary = {"ok": 0, "missing": 0, "drift": 0}
        for it in items:
            summary[it["status"]] = summary.get(it["status"], 0) + 1
        return {"ok": summary["missing"] == 0 and summary["drift"] == 0, "items": items, "summary": summary}

    @app.post("/api/suites/import")
    async def suite_import(req: Request):
        body = await req.json()
        name = body.get("name")
        data = body.get("data") or (yaml.safe_load(body["yaml"]) if body.get("yaml") else None)
        if not name or data is None:
            raise HTTPException(400, "need {name, yaml|data}")
        repo.upsert_suite(name, data)
        return {"ok": True, "name": name}

    # Manual tests are Markdown repo artifacts (atf_checks/<model>/<vector>/<id>.md), discovered
    # like code checks — no DB-backed manual-check API.

    # ---- test plans (suite + bench/board) ----
    @app.get("/api/test-plans")
    def test_plans():
        return repo.list_test_plans()

    @app.get("/api/test-plans/{name}")
    def test_plan_get(name: str):
        tp = repo.get_test_plan(name)
        if tp is None:
            raise HTTPException(404, "test plan not found")
        return tp

    @app.put("/api/test-plans/{name}")
    def test_plan_put(name: str, body: dict = Body(...)):
        repo.upsert_test_plan(name, body)
        return {"ok": True, "name": name}

    @app.delete("/api/test-plans/{name}")
    def test_plan_delete(name: str):
        repo.delete_test_plan(name)
        return {"ok": True}

    @app.post("/api/test-plans/capabilities")
    def test_plan_capabilities(body: dict = Body(...), _=Depends(require_login)):
        """Which driver/action capabilities the plan's resolved tests need vs what the target board
        provides — so the UI can flag tests that would SKIP before the run."""
        from atf.core.registry import resolve_selection
        from atf.core.runner import available_actions, available_drivers
        s = repo.get_suite(body.get("suite")) or {}
        specs = resolve_selection(s.get("select") or {})
        need_d, need_a = set(), set()
        for sp in specs:
            need_d |= set(sp.drivers)
            need_a |= set(sp.actions)
        have_d, have_a = set(), set()
        try:
            bench = repo.inventory_bench(body.get("bench"))
            board = next((b for b in bench.boards if b.name == body.get("board")), None)
            if board:
                have_d, have_a = available_drivers(bench, board), available_actions(bench, board)
        except Exception:
            pass
        return {"need_drivers": sorted(need_d), "need_actions": sorted(need_a),
                "have_drivers": sorted(have_d), "have_actions": sorted(have_a),
                "missing_drivers": sorted(need_d - have_d), "missing_actions": sorted(need_a - have_a)}

    # ---- config: requirements (catalogs). DB is source of truth; YAML import/export ----
    @app.get("/api/requirements")
    def requirements(framework: str | None = None):
        return repo.list_requirements(framework)

    @app.get("/api/requirements/frameworks")
    def requirement_frameworks():
        fsrc = _framework_sources(repo)                # framework -> [live source labels]
        return [{**f, "synced": f["framework"] in fsrc, "sources": fsrc.get(f["framework"], [])}
                for f in repo.frameworks()]

    @app.post("/api/frameworks")
    def framework_create(body: dict = Body(...)):
        fw = (body.get("framework") or "").strip()
        if not fw:
            raise HTTPException(400, "need framework")
        repo.upsert_catalog(fw, body.get("title", ""))
        reqmeta.invalidate()
        return {"ok": True, "framework": fw}

    @app.delete("/api/frameworks/{framework}")
    def framework_delete(framework: str):
        repo.delete_framework(framework)
        reqmeta.invalidate()
        return {"ok": True}

    @app.post("/api/requirements")
    def requirement_upsert(body: dict = Body(...)):
        fw, code = (body.get("framework") or "").strip(), (body.get("code") or "").strip()
        if not fw or not code:
            raise HTTPException(400, "need framework + code")
        repo.upsert_requirement(fw, code, {"title": body.get("title", ""),
                                           "desc": body.get("description", ""),
                                           "verify": body.get("verify", ""),
                                           "priority": body.get("priority")})
        reqmeta.invalidate()
        return {"ok": True, "id": f"{fw}:{code}"}

    @app.delete("/api/requirements/{framework}/{code}")
    def requirement_delete(framework: str, code: str):
        repo.delete_requirement(framework, code)
        reqmeta.invalidate()
        return {"ok": True}

    @app.post("/api/requirements/import")
    async def requirements_import(req: Request):
        body = await req.json()
        text = body.get("yaml")
        if text is None:
            raise HTTPException(400, "need {yaml}")
        fw, title, parsed = reqmeta.parse_yaml(text)
        fw = (body.get("framework") or fw or "").strip()
        if not fw:
            raise HTTPException(400, "no framework in the YAML")
        repo.import_requirements(fw, parsed, title=title)
        reqmeta.invalidate()
        return {"ok": True, "framework": fw, "count": len(parsed)}

    @app.get("/api/requirements/{framework}/export")
    def requirements_export(framework: str):
        rows = repo.list_requirements(framework)
        if not rows and framework not in {f["framework"] for f in repo.frameworks()}:
            raise HTTPException(404, "framework not found")
        title = next((f["title"] for f in repo.frameworks() if f["framework"] == framework), "")
        text = reqmeta.dump_yaml(framework, title, rows)
        return PlainTextResponse(text, media_type="application/x-yaml",
                                 headers={"Content-Disposition": f'attachment; filename="{framework}.yaml"'})

    # ---- config: checks (code lives in files; the API lists + scaffolds) ----
    @app.post("/api/checks/new")
    def check_new(body: dict = Body(...)):
        import importlib

        from atf.core import scaffold
        if not body.get("id"):
            raise HTTPException(400, "need an id")
        try:
            path = scaffold.new_check(
                id=body["id"], driver=body.get("driver", "host"),
                actions=[a.strip() for a in (body.get("actions") or []) if str(a).strip()]
                if isinstance(body.get("actions"), list)
                else [a.strip() for a in str(body.get("actions") or "").split(",") if a.strip()],
                severity=body.get("severity", "medium"), title=body.get("title", ""),
                model=body.get("model", "common"))
        except (ValueError, FileExistsError) as e:
            raise HTTPException(400, str(e))
        module = str(path).replace("/", ".")[:-3]      # atf_checks/common/mgmt/x.py -> atf_checks.common.mgmt.x
        try:                                           # live-register so it appears immediately
            importlib.import_module(module)
        except Exception:
            pass
        return {"ok": True, "path": str(path),
                "note": "edit the module's TODOs; mgmt checks need `make image` before running"}

    # ---- check source: read / edit the .py of an auto check (files are source of truth) ----
    def _resolve_check_file(id: str):
        import inspect
        import sys
        from atf.core.registry import REGISTRY
        spec = REGISTRY.get(id)
        if spec is None:
            raise HTTPException(404, f"unknown check: {id}")
        if spec.mode == "manual":                       # a Markdown manual test — edited as the .md file
            p = Path(getattr(spec, "path", "") or "")
            if not p.is_file():
                raise HTTPException(404, "manual test source file not found")
            return "(manual)", p.resolve()
        mod_name = getattr(spec.fn, "__module__", "")
        if not mod_name.startswith("atf_checks."):
            raise HTTPException(409, f"{id} has no editable source file")
        mod = sys.modules.get(mod_name)
        path = inspect.getsourcefile(mod) if mod else None
        if not path or not Path(path).is_file():
            raise HTTPException(404, "source file not found")
        return mod_name, Path(path).resolve()

    def _rel(path: Path) -> str:
        try:
            return str(path.relative_to(Path.cwd()))
        except ValueError:
            return str(path)

    def _editable_path(abs_path: Path) -> bool:
        """A file is editable in place only when it isn't inside a server-managed git checkout — a
        `git` Repository's clone is transient (Sync overwrites it). `path` sources and local
        ($ATF_CHECK_SOURCES) dirs are the user's real directories → editable."""
        rf = str(abs_path.resolve())
        for s in (repo.list_check_sources() if repo else []):
            co = s.get("checkout")
            if co and s.get("last_status") == "ok" and rf.startswith(str(Path(co).resolve()) + "/"):
                return (s.get("kind") or "git") != "git"
        return True                                     # a local $ATF_CHECK_SOURCES dir

    @app.get("/api/checks/{id}/source")
    def check_source_get(id: str):
        mod_name, path = _resolve_check_file(id)
        return {"id": id, "module": mod_name, "path": _rel(path), "manual": mod_name == "(manual)",
                "editable": _editable_path(path),
                "abs_path": str(path), "source": path.read_text()}

    @app.put("/api/checks/{id}/source")
    def check_source_put(id: str, body: dict = Body(...), _=Depends(require_admin)):
        import importlib
        import sys
        import traceback
        from atf.core.registry import REGISTRY
        mod_name, path = _resolve_check_file(id)
        if not _editable_path(path):                     # a git Repository's clone is transient
            raise HTTPException(409, "read-only: this test is synced from a git repo — edit it in the "
                                     "repo, push, then Sync")
        new_src = body.get("source")
        if new_src is None:
            raise HTTPException(400, "need {source}")
        if mod_name == "(manual)":                       # a Markdown test — write the .md + re-discover
            from atf.core.checks import reload_upstream
            path.write_text(new_src)
            reload_upstream()
            return {"ok": True, "path": _rel(path)}
        try:                                            # never persist un-parseable code
            compile(new_src, str(path), "exec")
        except SyntaxError as e:
            return {"ok": False, "error": f"SyntaxError: {e.msg} (line {e.lineno})"}

        def _reload():
            owned = [cid for cid, s in list(REGISTRY.items())
                     if getattr(s.fn, "__module__", "") == mod_name]
            for cid in owned:
                REGISTRY.pop(cid, None)
            mod = sys.modules.get(mod_name)
            importlib.reload(mod) if mod else importlib.import_module(mod_name)

        old_src = path.read_text()
        path.write_text(new_src)
        try:                                            # atf.core.checks imports wholesale at boot —
            _reload()                                   # a broken module must never reach disk
        except Exception:
            path.write_text(old_src)
            try:
                _reload()
            except Exception:
                pass
            return {"ok": False, "error": traceback.format_exc(limit=4)}
        return {"ok": True, "path": _rel(path)}

    @app.post("/api/checks/{id}/open-ide")
    def check_open_ide(id: str, _=Depends(require_admin)):
        import shutil
        import subprocess
        _mod, path = _resolve_check_file(id)
        exe = shutil.which("code") or shutil.which("code-insiders")
        if not exe:
            return {"ok": True, "launched": False, "path": _rel(path),
                    "note": "`code` not on PATH — open the file manually"}
        try:
            subprocess.Popen([exe, "-g", str(path)])
            return {"ok": True, "launched": True, "path": _rel(path)}
        except Exception as e:
            return {"ok": True, "launched": False, "path": _rel(path), "note": str(e)}

    # ---- workbench: run one check / scratch-exec against a live board (admin) ----
    def _bench_board(bench_name: str, board_name: str):
        bench = state._load_bench(bench_name or "")
        bd = next((b for b in bench.boards if b.name == board_name), None)
        if bd is None:
            raise HTTPException(404, f"board not in bench: {board_name}")
        return bench, bd

    @app.post("/api/dev/run")
    def dev_run(body: dict = Body(...), _=Depends(require_admin)):
        import shutil
        import tempfile
        from atf.core import runner
        from atf.core.registry import REGISTRY
        cid, board = body.get("id"), body.get("board")
        if not cid or not board:
            raise HTTPException(400, "need {id, board}")
        spec = REGISTRY.get(cid)
        if spec is None:
            raise HTTPException(404, f"unknown check: {cid}")
        if spec.mode == "manual":
            raise HTTPException(409, "manual checks run via the pilot, not dev-run")
        bench, _bd = _bench_board(body.get("bench"), board)
        import time as _t
        run_id = f"dev-{int(_t.time() * 1000)}"
        ok, conflict = locks.acquire(touched_resources(bench, {board}), run_id, who=f"dev-run:{cid}")
        if not ok:
            raise HTTPException(409, "bench busy — " + "; ".join(
                f"{r} in use by {h['who']}" for r, h in conflict.items()))
        tmp = Path(tempfile.mkdtemp(prefix="atf-devrun-"))
        try:
            recs = runner.run(bench, [spec], tmp, boards_filter={board},
                              mgmt_backend=body.get("mgmt_backend", "local"))
            out = []
            for r in recs:
                ev = ""
                if r.evidence and (tmp / r.evidence).is_file():
                    ev = (tmp / r.evidence).read_text()[:20000]
                out.append({"check": r.check, "board": r.board, "drivers": r.drivers, "actions": r.actions,
                            "verdict": r.verdict, "severity": r.severity, "title": r.title,
                            "detail": r.detail, "evidence": ev, "metrics": r.metrics})
            return {"ok": True, "records": out}
        finally:
            locks.release(run_id)
            shutil.rmtree(tmp, ignore_errors=True)

    @app.post("/api/dev/exec")
    def dev_exec(body: dict = Body(...), _=Depends(require_admin)):
        import contextlib
        import io
        import shutil
        import tempfile
        import traceback
        from atf.core import model, runner
        code, board = body.get("code"), body.get("board")
        if not code or not board:
            raise HTTPException(400, "need {board, code}")
        bench, bd = _bench_board(body.get("bench"), board)
        tmp = Path(tempfile.mkdtemp(prefix="atf-devexec-"))
        ctx = runner._build_ctx(bench, bd, tmp)
        ctx.check_id = "scratch"
        try:                                            # wire ip-without-agent drivers locally so ctx.<alias> works
            from atf.access.channels.ip import IpChannel
            for alias, cfg in (bd.drivers or {}).items():
                if isinstance(cfg, dict) and cfg.get("type") == "ip" and not cfg.get("agent"):
                    ctx.drivers[alias] = IpChannel(cfg, None, bd.creds)
        except Exception:
            pass
        g = {"ctx": ctx, "board": bd, "host": ctx.host,
             "Result": model.Result, "Verdict": model.Verdict, "Severity": model.Severity}
        buf = io.StringIO()
        result, err = None, None
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                result = _exec_last_expr(code, g)
        except Exception:
            err = traceback.format_exc(limit=6)
        finally:
            for ch in [*ctx.drivers.values(), ctx.actions]:
                if ch is not None:
                    try:
                        ch.close()
                    except Exception:
                        pass
            ev = ""
            evdir = tmp / "evidence"
            if evdir.is_dir():
                ev = "\n".join(f"--- {p.name} ---\n{p.read_text()[:4000]}"
                               for p in sorted(evdir.glob("*")))
            shutil.rmtree(tmp, ignore_errors=True)
        return {"ok": err is None, "stdout": buf.getvalue()[:20000],
                "result": result, "error": err, "evidence": ev}

    # ---- dev/host agents (Mode A: run a developer's local working tree on the bench) ----
    def _own_agent(aid, me):
        """Fetch an agent and check the caller may manage it (its owner, or an admin)."""
        s = hub.get(aid)
        if s is None:
            raise HTTPException(404, "unknown agent")
        if not (me.get("is_admin") or getattr(s, "owner", "") == me["username"]):
            raise HTTPException(403, "not your agent")
        return s

    @app.get("/api/agents/token")                       # YOUR enrollment token (private to you)
    def agent_token_get(me=Depends(require_login)):
        return {"token": repo.user_agent_token(me["username"])}

    @app.post("/api/agents/token")                      # rotate YOUR token
    def agent_token_rotate(me=Depends(require_login)):
        return {"token": repo.rotate_user_agent_token(me["username"])}

    @app.get("/agent.py")                               # zero-install: tester curls + runs this
    def agent_script():
        from atf import agent as _agentmod
        return PlainTextResponse(
            Path(_agentmod.__file__).read_text(), media_type="text/x-python",
            headers={"Content-Disposition": 'attachment; filename="atf-agent.py"'})

    @app.post("/api/agents/register")                   # agent → server (per-user enrollment token)
    def agent_register(body: dict = Body(...)):
        owner = repo.user_of_agent_token(body.get("token", ""))
        if not owner:
            raise HTTPException(401, "bad agent token")
        s = hub.register(body.get("name", "agent"), body.get("sources"), body.get("vantages"),
                         body.get("platform", ""), body.get("catalog"), body.get("req_files"), owner=owner)
        return {"id": s.id}

    @app.get("/api/agents/{aid}/poll")                  # agent long-polls for commands
    def agent_poll(aid: str):
        import queue as _q
        s = hub.get(aid)
        if s is None:
            raise HTTPException(404, "unknown agent")
        s.touch()
        try:
            return s.cmds.get(timeout=25)
        except _q.Empty:
            return {"cmd": "noop"}

    @app.post("/api/agents/{aid}/tree")                 # agent uploads its working tree (tar.gz)
    async def agent_tree(aid: str, token: str, request: Request):
        s = hub.get(aid)
        if s is None:
            raise HTTPException(404, "unknown agent")
        hub.receive_tree(s, token, await request.body())
        return {"ok": True}

    @app.post("/api/agents/{aid}/inspect")              # agent uploads its tree/diff (JSON)
    async def agent_inspect_recv(aid: str, token: str, body: dict = Body(...)):
        s = hub.get(aid)
        if s is None:
            raise HTTPException(404, "unknown agent")
        hub.receive_inspect(s, token, body.get("sources") or [])
        return {"ok": True}

    @app.post("/api/agents/{aid}/catalog")              # agent uploads its check catalog (JSON)
    async def agent_catalog_recv(aid: str, token: str, body: dict = Body(...)):
        s = hub.get(aid)
        if s is None:
            raise HTTPException(404, "unknown agent")
        hub.receive_catalog(s, token, body.get("catalog") or [], body.get("req_files"))
        return {"ok": True}

    @app.post("/api/agents/{aid}/file")                 # agent returns a file read/write result
    async def agent_file_recv(aid: str, token: str, body: dict = Body(...)):
        s = hub.get(aid)
        if s is None:
            raise HTTPException(404, "unknown agent")
        hub.receive_file(s, token, body or {})
        return {"ok": True}

    def _agent_check(s, cid):
        c = next((x for x in (s.catalog or []) if x.get("id") == cid), None)
        if c is None:
            raise HTTPException(404, f"'{cid}' is not in this agent's catalog")
        if not c.get("source") or not c.get("path"):
            raise HTTPException(409, "this agent is an older build without code view/edit — "
                                     "re-download it (curl <server>/agent.py) and reconnect")
        return c

    @app.get("/api/agents/{aid}/checks/{cid}/source")   # view an agent check's code (admin)
    def agent_check_source(aid: str, cid: str, me=Depends(require_login)):
        s = _own_agent(aid, me)
        c = _agent_check(s, cid)
        tok, ev = hub.request_file(s, "read", c["source"], c["path"])
        if not ev.wait(timeout=20) or not s.files[tok]["data"]:
            raise HTTPException(504, "agent did not return the file")
        d = s.files[tok]["data"]
        return {"id": cid, "agent": s.name, "path": f"{c['source']}/{c['path']}",
                "source": d.get("content", "")}

    @app.put("/api/agents/{aid}/checks/{cid}/source")   # edit it, written back on the tester's box
    def agent_check_source_put(aid: str, cid: str, body: dict = Body(...), me=Depends(require_login)):
        s = _own_agent(aid, me)
        c = _agent_check(s, cid)
        if body.get("source") is None:
            raise HTTPException(400, "need {source}")
        tok, ev = hub.request_file(s, "write", c["source"], c["path"], body["source"])
        if not ev.wait(timeout=20) or s.files[tok]["data"] is None:
            raise HTTPException(504, "agent did not confirm the write")
        return s.files[tok]["data"]                      # {ok:true} or {ok:false, error}

    _MANUAL_MD_BODY = ("## Objetivo\n\n_(o que este teste verifica, em caixa-preta)_\n\n"
                       "## Pré-condições\n\n- \n\n## Setup (opcional)\n\n_(preparação antes do teste)_\n\n"
                       "## Passos\n\n1. \n\n## Observações\n\n- [ ] \n\n"
                       "## Teardown (opcional)\n\n_(restaurar o estado — ex.: religar a placa)_\n\n"
                       "## Veredito\n\n- **pass** se …\n- **gap** se …\n")

    def _agent_write(s, source, path, content):
        tok, ev = hub.request_file(s, "write", source, path, content)
        if not ev.wait(timeout=20) or s.files[tok]["data"] is None:
            raise HTTPException(504, "agent did not confirm the write")
        d = s.files[tok]["data"]
        if not d.get("ok"):
            raise HTTPException(400, d.get("error", "write failed"))
        return d

    def _csv_list(v):
        return ([x.strip() for x in v if str(x).strip()] if isinstance(v, list)
                else [x.strip() for x in str(v or "").split(",") if x.strip()])

    @app.post("/api/agents/{aid}/manual")               # scaffold a Markdown manual test on the agent repo
    def agent_manual_new(aid: str, body: dict = Body(...), me=Depends(require_login)):
        s = _own_agent(aid, me)
        cid = (body.get("id") or "").strip()
        source = (body.get("source") or "").strip()
        if not cid or not source:
            raise HTTPException(400, "need {id, source}")
        model = (body.get("model") or "common").strip() or "common"
        drivers, actions = _csv_list(body.get("drivers")), _csv_list(body.get("actions"))
        fm = ["---", f"id: {cid}", f"title: {body.get('title') or cid}",
              f"severity: {body.get('severity') or 'medium'}",
              f"drivers: [{', '.join(drivers)}]", f"actions: [{', '.join(actions)}]"]
        if body.get("disruptive"):
            fm.append("disruptive: true")
        fm.append("---")
        md = "\n".join(fm) + "\n\n" + (body.get("body") or _MANUAL_MD_BODY).rstrip() + "\n"
        path = f"atf_checks/{model}/manual/{cid}.md"   # manuals live under <model>/manual/, not a driver dir
        _agent_write(s, source, path, md)
        return {"ok": True, "id": cid, "path": path, "source": source}

    @app.post("/api/agents/{aid}/ai")                   # turn AI (Claude) on/off for an owned agent
    def agent_ai(aid: str, body: dict = Body(...), authorization: str = Header(default=""),
                 me=Depends(require_login)):
        s = _own_agent(aid, me)
        if not body.get("on"):
            s.ai = {"on": False, "path": s.ai.get("path", ""), "claude": None,
                    "unrestricted": s.ai.get("unrestricted", True), "model": s.ai.get("model", "")}
            return {"ok": True, "ai": s.ai}
        if s.ai.get("on") and not body.get("path") and ("unrestricted" in body or "model" in body):
            if "unrestricted" in body:                              # mode/model update, no re-install
                s.ai["unrestricted"] = bool(body.get("unrestricted"))
            if "model" in body:
                s.ai["model"] = (body.get("model") or "").strip()
            return {"ok": True, "ai": s.ai}
        path = (body.get("path") or "~/atf-ai").strip()
        import base64
        import io
        import tarfile
        pack_dir = Path(__file__).resolve().parent.parent / "agent_pack"
        if not pack_dir.is_dir():
            raise HTTPException(500, "resource pack not found on the server")
        buf = io.BytesIO()
        def _pack_only(ti):                             # ship sources, never compiled bytecode
            parts = ti.name.split("/")
            return None if ("__pycache__" in parts or parts[-1].endswith(".pyc")) else ti
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(pack_dir, arcname=".", filter=_pack_only)   # CLAUDE.md + .claude/skills/…
        utok = authorization[7:].strip() if authorization[:7].lower() == "bearer " else ""
        tok, ev = hub.request_ai(s, path, base64.b64encode(buf.getvalue()).decode(), "", utok)
        if not ev.wait(timeout=25) or s.files[tok]["data"] is None:
            raise HTTPException(504, "agent did not confirm AI enable")
        d = s.files[tok]["data"]
        if not d.get("ok"):
            raise HTTPException(400, d.get("error", "ai enable failed"))
        s.ai = {"on": True, "path": d.get("path", path), "claude": d.get("claude"),
                "unrestricted": bool(body.get("unrestricted", True)), "model": (body.get("model") or "").strip()}
        return {"ok": True, "ai": s.ai, "files": d.get("files", [])}

    @app.post("/api/agents/{aid}/ai-run")               # dispatch a headless Claude prompt → returns a job
    def agent_ai_run(aid: str, body: dict = Body(...), me=Depends(require_login)):
        s = _own_agent(aid, me)
        if not (s.ai or {}).get("on"):
            raise HTTPException(400, "AI is off for this agent — enable it first")
        prompt = (body.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(400, "need a prompt")
        # ASYNC: kick off the run and return a job token immediately — the client polls for the
        # result. Holding the HTTP request for the whole (minutes-long) claude run made browsers/
        # proxies drop the idle connection ("NetworkError").
        tok, _ev = hub.request_ai_run(s, s.ai.get("path", ""), prompt, resume=body.get("session") or "",
                                      unrestricted=bool(s.ai.get("unrestricted", True)),
                                      model=s.ai.get("model", ""))
        return {"job": tok}

    @app.get("/api/agents/{aid}/ai-run/{job}")          # poll a dispatched run
    def agent_ai_run_poll(aid: str, job: str, me=Depends(require_login)):
        s = _own_agent(aid, me)
        it = s.files.get(job)
        if it is None:
            raise HTTPException(404, "unknown or expired job")
        if it["data"] is None:
            return {"pending": True}
        data = s.files.pop(job)["data"]                  # deliver once, then free it
        return {"pending": False, "result": data}        # result = {ok, out, session, meta, err} | {ok:false, error}

    @app.post("/api/agents/{aid}/scaffold")             # scaffold an auto-test .py on the agent repo
    def agent_scaffold_new(aid: str, body: dict = Body(...), me=Depends(require_login)):
        s = _own_agent(aid, me)
        from atf.core import scaffold
        cid = (body.get("id") or "").strip()
        source = (body.get("source") or "").strip()
        if not cid or not source:
            raise HTTPException(400, "need {id, source}")
        model = (body.get("model") or "common").strip() or "common"
        try:
            slug, ddir, text = scaffold.render_check(
                id=cid, drivers=_csv_list(body.get("drivers")), actions=_csv_list(body.get("actions")),
                severity=body.get("severity", "medium"), title=body.get("title", ""))
        except ValueError as e:
            raise HTTPException(400, str(e))
        path = f"atf_checks/{model}/{ddir}/{slug}.py"
        _agent_write(s, source, path, text)
        return {"ok": True, "id": cid, "path": path, "source": source}

    def _parse_agent_reqs(req_files, origin) -> list:
        """Parse an agent's raw requirements/*.yaml (server has yaml) → full requirement rows tagged
        with the agent origin + its source repo. Overlay-only — never written to the DB."""
        out = []
        for rf in (req_files or []):
            try:
                fw, _title, parsed = reqmeta.parse_yaml(rf.get("yaml", ""))
                fw = fw or Path(rf.get("name", "")).stem
                src = rf.get("source", "")
                for code, mrow in parsed.items():
                    row = {"id": f"{fw}:{code}", "framework": fw, "code": code,
                           "title": mrow.get("title", ""), "desc": mrow.get("desc", ""),
                           "verify": mrow.get("verify", ""), "priority": mrow.get("priority"),
                           "source": src, "origin": origin}
                    out.append({**row, "sha": reqmeta.requirement_sha(row)})
            except Exception:
                pass
        return out

    @app.get("/api/agents")                             # any logged-in user: connected agents (with owner)
    def agents_list(_=Depends(require_login)):
        return [s.info() for s in hub.alive()]

    @app.get("/api/agents/catalog")                     # any logged-in user: agents' checks (shared overlay)
    def agents_catalog(_=Depends(require_login)):
        out = []
        for s in hub.alive():
            checks = s.catalog                          # cached; try a fresh pull, short-bounded
            try:
                tok, ev = hub.request_catalog(s)
                if ev.wait(timeout=8) and s.catalogs[tok]["data"] is not None:
                    checks = s.catalogs[tok]["data"]
            except Exception:
                pass
            # delta = the file on the agent changed since the app last loaded it (connect/Sync)
            out.append({"id": s.id, "name": s.name,
                        "checks": [{**c, "origin": f"agent:{s.name}",
                                    "delta": c.get("sha") != s.loaded.get(c.get("path"))}
                                   for c in (checks or [])],
                        "requirements": _parse_agent_reqs(s.req_files, f"agent:{s.name}"),
                        "req_files": s.req_files})
        return {"agents": out}

    @app.get("/api/agents/{aid}/inspect")               # admin/UI: pull the agent's tree + diff
    def agent_inspect(aid: str, me=Depends(require_login)):
        s = _own_agent(aid, me)
        tok, ev = hub.request_inspect(s)
        if not ev.wait(timeout=25):
            raise HTTPException(504, "agent did not respond to inspect")
        return {"ok": True, "agent": s.name, "sources": s.inspects[tok]["data"]}

    @app.post("/api/agents/{aid}/run")                  # admin/UI: run the agent's working tree
    def agent_run(aid: str, body: dict = Body(...), me=Depends(require_login)):
        import shutil
        s = _own_agent(aid, me)
        board = body.get("board")
        if not board:
            raise HTTPException(400, "need {board}")
        import time as _t
        _bench = state._load_bench(Path(bench_path).stem)
        run_id = f"agent-{s.name}-{int(_t.time() * 1000)}"
        ok, conflict = locks.acquire(touched_resources(_bench, {board}), run_id, who=f"agent:{s.name}")
        if not ok:
            raise HTTPException(409, "bench busy — " + "; ".join(
                f"{r} in use by {h['who']}" for r, h in conflict.items()))
        out = None
        try:
            records, dev_dirs, out = _agent_worker_run(
                hub, s, Path(bench_path).stem, board,
                {"ids": body.get("ids"), "req": body.get("req"), "vectors": body.get("vectors")},
                body.get("mgmt_backend", "local"))
            for r in records:
                rel = r.get("evidence")
                r["evidence_text"] = ((out / rel).read_text()[:20000]
                                      if rel and (out / rel).is_file() else "")
            return {"ok": True, "agent": s.name, "sources": dev_dirs, "records": records}
        except RuntimeError as e:
            raise HTTPException(504 if "upload" in str(e) else 500, str(e))
        finally:
            locks.release(run_id)
            if out:
                shutil.rmtree(out, ignore_errors=True)

    @app.post("/api/agents/{aid}/sync")                 # admin: (re)load the agent's artifacts into the app
    def agent_sync(aid: str, me=Depends(require_login)):
        """Pull the agent's current tree into the platform and snapshot it as 'loaded' — clears the
        app↔filesystem delta until the tester edits a file again."""
        s = _own_agent(aid, me)
        tok, ev = hub.request_catalog(s)                # refreshes s.catalog (+ req_files) LIVE
        ev.wait(timeout=10)
        s.snapshot()                                    # mark the current catalog as what the app loaded
        itok, iev = hub.request_inspect(s)
        sources = s.inspects[itok]["data"] if iev.wait(timeout=25) else None
        return {"ok": True, "agent": s.name,
                "checks": len(s.catalog or []), "requirements": len(s.req_files or []),
                "sources": [{"name": x.get("name"), "sha1": x.get("head")} for x in (sources or [])]}

    @app.delete("/api/agents/{aid}")                    # admin: disconnect — clears its overlay
    def agent_disconnect(aid: str, me=Depends(require_login)):
        s = _own_agent(aid, me)                          # owner or admin only
        s.cmds.put({"cmd": "stop"})                      # ask the agent to exit (so it won't re-register)
        return {"ok": hub.drop(aid)}

    # ---- bench resource locks (a run holds the boards + agents it touches) ----
    @app.get("/api/locks")
    def locks_list():
        return locks.held()

    @app.delete("/api/locks/{resource}")                # forced unlock — emergency escape (admin)
    def locks_force(resource: str, _=Depends(require_admin)):
        return {"ok": locks.force_release(resource)}

    return app


def serve(out_root: Path, bench_path: str = "benches/lab.yaml",
          host: str = "127.0.0.1", port: int = 8899) -> int:
    import uvicorn

    from atf.store import open_repo
    repo = open_repo()
    try:
        migrated = repo.migrate_benches_to_inventory()   # move old per-bench agents/boards → inventory
        if migrated:
            print(f"migrated {migrated} bench(es) to the shared inventory")
        drv_migrated = repo.migrate_drivers_to_inventory()   # fold legacy console/craft/mgmt wiring → driver entities
        if drv_migrated:
            print(f"migrated {drv_migrated} driver binding(s) to inventory entities")
        _seed_from_files(repo)
        repo.ensure_admin()
        _seed_inventory(repo)                           # populate shared inventory from benches
        if repo.list_check_sources():                   # server mode: sync upstream repos (tolerant)
            from atf.core.checks import sync_sources
            res = sync_sources(repo)
            bad = [r["name"] for r in res if r.get("status") == "error"]
            if bad:
                print(f"⚠ could not sync check repos: {', '.join(bad)} — relying on connected agents")
            _discover_checks()                          # reload checks from synced checkouts
        _reload_requirements(repo)                      # requirements = configured sources only (cleans leftovers)
        from atf.core.registry import REGISTRY
        print(f"upstream checks available: {len([1 for _ in REGISTRY])}")
    except Exception:
        pass
    app = build_app(out_root, bench_path, repo)
    origin = os.environ.get("PUBLIC_HOST")
    if origin:                                        # allow the browser on the test server
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=[f"http://{origin}:{port}"],
                           allow_methods=["*"], allow_headers=["*"])
    print(f"atf backend → http://{host}:{port}   (store: {out_root}, db: {os.environ.get('DATABASE_URL','reports/atf.db')})")
    print(f"API docs → http://{host}:{port}/api/docs")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0
