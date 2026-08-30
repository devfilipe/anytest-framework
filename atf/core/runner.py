"""Select + dispatch checks per board. Host/console/craft run in-process;
`mgmt` checks are batched to the atf-mgmt container (or run locally for dev)."""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from atf.access.host import HostProbe
from atf.core.inventory import Bench, Board
from atf.core.model import CheckSpec, Ctx, Result, Severity, Verdict

DEFAULT_MGMT_IMAGE = "atf-mgmt:latest"


def model_slug(name: str) -> str:
    """The check-namespace slug for a board model name. DB-first (the configurable board_model
    mapping — several models may share a slug, e.g. Router-X/Router-X Lite → router-x); falls back to
    a normalization of the name when no mapping is configured."""
    try:
        from atf.store import open_repo
        s = open_repo().board_model_slug(name)
        if s:
            return s
    except Exception:
        pass
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")


@dataclass
class Record:
    run_id: str
    ts: str
    board: str
    check: str
    requirements: list[str]
    verdict: str
    severity: str
    title: str
    detail: str
    evidence: str
    metrics: dict = field(default_factory=dict)
    drivers: list[str] = field(default_factory=list)   # comm drivers the test declared
    actions: list[str] = field(default_factory=list)   # node actions the test declared
    suite: str = ""          # the plan that produced this run ("" = ad-hoc filters)
    bench: str = ""          # the inventory it ran against (bench file stem)
    source: str = ""         # check-source repo the test was loaded from (provenance)


def _build_ctx(bench: Bench, board: Board, out_root: Path) -> Ctx:
    """Per-board context, alias-keyed. In-process drivers: `serial` (console) and `ip`-with-agent
    (old craft). An `ip` driver WITHOUT an agent (the management/nmap vantage) is built in the
    worker, not here."""
    from atf.access.agent import AgentConn
    drivers: dict = {}
    for alias, cfg in (board.drivers or {}).items():
        typ, agent = cfg.get("type"), cfg.get("agent")
        if typ == "serial":
            if agent in bench.agents:
                from atf.access.channels.console import SerialChannel
                drivers[alias] = SerialChannel(AgentConn(bench.agents[agent]), cfg)
        elif agent and agent in bench.agents:          # ip-like driver with an agent (old craft vantage)
            from atf.access.channels.ip import IpChannel
            drivers[alias] = IpChannel(cfg, AgentConn(bench.agents[agent]), board.creds)
        # ip-like without an agent → handled by the worker (container/host nmap vantage)
    from atf.access.actions import Actions
    acfg = getattr(board, "actions", None)
    actions = Actions(acfg, bench.agents) if acfg else None
    return Ctx(board=board, host=HostProbe(), out_root=out_root, drivers=drivers, actions=actions)


def _severity(res: Result, spec: CheckSpec) -> Severity:
    if res.severity is not None:
        return res.severity
    return spec.severity if res.verdict == Verdict.GAP else Severity.INFO


def run_check(spec: CheckSpec, ctx: Ctx) -> Result:
    """Run one check against ctx, never raising (a crash becomes an ERROR result).
    Guarantees every determinate result carries an evidence file — even error paths —
    so the report always has something to embed."""
    ctx.check_id = spec.id
    missing = spec.drivers - ctx.available_drivers
    if missing:
        return Result(Verdict.SKIPPED, detail=f"missing drivers: {sorted(missing)}")
    missing_a = spec.actions - ctx.available_actions
    if missing_a:
        return Result(Verdict.SKIPPED, detail=f"missing actions: {sorted(missing_a)}")
    try:
        res = spec.fn(ctx)
    except Exception as e:  # a check must never crash the run
        res = Result(Verdict.ERROR, detail=f"{type(e).__name__}: {e}")
    if not res.evidence:
        try:
            res.evidence = ctx.write_evidence(
                res.detail or res.title or f"(no output; verdict={res.verdict.value})")
        except Exception:
            pass
    return res


def result_dict(spec: CheckSpec, res: Result) -> dict:
    """Flatten (spec, Result) into the wire/record dict (shared host↔worker)."""
    return {
        "check": spec.id,
        "requirements": list(spec.requirements),
        "drivers": sorted(spec.drivers),
        "actions": sorted(spec.actions),
        "verdict": res.verdict.value,
        "severity": _severity(res, spec).value,
        "title": res.title or spec.title,
        "detail": res.detail,
        "evidence": res.evidence,
        "metrics": res.metrics,
    }


def _record(run_id: str, board_name: str, d: dict, suite: str = "", bench: str = "") -> Record:
    return Record(run_id=run_id, ts=datetime.now().isoformat(timespec="seconds"),
                  board=board_name, suite=suite, bench=bench, **d)


def available_drivers(bench: Bench, board: Board) -> set:
    """Which driver ALIASES this board exposes on this bench. A `serial` driver (or an `ip` driver
    that names an agent) needs that agent wired on the bench; an `ip` driver WITHOUT an agent (the
    host/container nmap vantage) counts as available once declared. A check whose required aliases
    aren't all here can't run → reported SKIPPED. (Reads `board.drivers`.)"""
    av = set()
    for alias, cfg in (getattr(board, "drivers", {}) or {}).items():
        if not isinstance(cfg, dict):
            continue
        agent = cfg.get("agent")
        if not agent:                        # ip-without-agent: reachable once declared
            av.add(alias)
        elif agent in bench.agents:          # serial / ip-with-agent: needs the wired agent
            av.add(alias)
    return av


def available_actions(bench: Bench, board: Board) -> set:
    """Node actions the bench configured for this board (agent + ≥1 signal). A check declaring an
    action the bench doesn't provide is SKIPPED. (Reads `board.actions`.)"""
    cfg = getattr(board, "actions", None) or {}
    from atf.access.actions import Actions
    return set(Actions(cfg, bench.agents).available())


def run(bench: Bench, specs: list[CheckSpec], out_root: Path,
        boards_filter: Optional[set[str]] = None,
        mgmt_backend: str = "docker", mgmt_image: str = DEFAULT_MGMT_IMAGE,
        suite: str = "", bench_name: str = "", on_record=None, cancel=None,
        suite_select: Optional[dict] = None, run_id: Optional[str] = None) -> list[Record]:
    """`on_record(rec)` (optional) is called after each check result — a progress hook for
    a live UI. It must not raise (a bad callback can't break the run). `cancel` (optional
    threading.Event) is checked between checks — set it to stop the run early. `run_id` lets the
    caller pre-assign the run identity (so a per-run artifact dir can be named before the run)."""
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    records: list[Record] = []
    boards = [b for b in bench.boards if not boards_filter or b.name in boards_filter]

    # version provenance — record which revision of each check source produced this run, and map
    # every test back to its source repo (so the report can show "test X @ commit, as loaded").
    from atf.core import checks as _checks
    src_versions = _checks.source_versions()
    _roots = sorted(((v["path"], v["name"]) for v in src_versions), key=lambda t: -len(t[0]))
    spec_by_id = {s.id: s for s in specs}

    def _src_of(cid: str) -> str:
        spec = spec_by_id.get(cid)
        mod = sys.modules.get(getattr(spec.fn, "__module__", "") if spec else "")
        f = getattr(mod, "__file__", None)
        if not f:
            return ""
        rf = str(Path(f).resolve())
        for root, label in _roots:
            if rf == root or rf.startswith(root + "/"):
                return label
        return ""

    def cancelled():
        return cancel is not None and cancel.is_set()

    def add(board_name, d):
        d.setdefault("source", _src_of(d.get("check", "")))
        r = _record(run_id, board_name, d, suite=suite, bench=bench_name)
        records.append(r)
        if on_record:
            try:
                on_record(r)
            except Exception:
                pass
        return r

    for board in boards:
        if cancelled():
            break
        # model gating: a board runs common checks (model=="") + checks for its own model only
        mslug = model_slug(board.model)
        bspecs = [s for s in specs if not s.model or not mslug or s.model == mslug]
        # capability availability: a check whose required driver/action this board lacks is SKIPPED
        # (unavailable), not silently dropped — drivers/actions belong to the bench, not the suite
        avail = available_drivers(bench, board)
        avail_a = available_actions(bench, board)
        for s in [s for s in bspecs if (s.drivers and not (set(s.drivers) <= avail))
                  or (s.actions and not (set(s.actions) <= avail_a))]:
            miss_d = sorted(set(s.drivers) - avail)
            miss_a = sorted(set(s.actions) - avail_a)
            need = ", ".join([f"driver '{d}'" for d in miss_d] + [f"action '{a}'" for a in miss_a])
            add(board.name, {"check": s.id, "requirements": list(s.requirements),
                             "drivers": sorted(s.drivers), "actions": sorted(s.actions),
                             "verdict": Verdict.SKIPPED.value, "severity": Severity.INFO.value,
                             "title": s.title, "metrics": {}, "evidence": "",
                             "detail": f"not runnable on this bench — {need} not configured for board {board.name}"})
        bspecs = [s for s in bspecs if (not s.drivers or set(s.drivers) <= avail)
                  and (not s.actions or set(s.actions) <= avail_a)]
        # Run in the SUITE'S DECLARED ORDER (setup → … → teardown). A check needs the atf-mgmt
        # container if and only if it declares an alias that resolves to an `ip` driver WITHOUT an agent
        # (the host/container nmap vantage). Consecutive such checks batch to one dispatch; anything
        # else runs in-process against the shared ctx. A non-container check between them splits the
        # batch so the sequence is preserved.
        container_aliases = {a for a, cfg in (board.drivers or {}).items()
                             if isinstance(cfg, dict) and cfg.get("type") != "serial" and not cfg.get("agent")}

        def needs_container(spec):
            return bool(set(spec.drivers) & container_aliases)

        ctx = _build_ctx(bench, board, out_root)
        try:
            i = 0
            while i < len(bspecs):
                if cancelled():
                    break
                if needs_container(bspecs[i]):
                    j = i
                    while j < len(bspecs) and needs_container(bspecs[j]):
                        j += 1
                    from atf.access.mgmt import dispatch as mgmt_dispatch
                    for d in mgmt_dispatch.dispatch(board, bspecs[i:j], out_root,
                                                   backend=mgmt_backend, image=mgmt_image):
                        add(board.name, d)
                    i = j
                else:
                    add(board.name, result_dict(bspecs[i], run_check(bspecs[i], ctx)))
                    i += 1
        finally:
            for ch in [*ctx.drivers.values(), ctx.actions]:
                if ch is not None:
                    ch.close()

    # run metadata: the bench under test (element/board identity) + the check-source versions loaded.
    # The report reads this to present *what was tested, where, and at which revision*.
    out_root.mkdir(parents=True, exist_ok=True)
    meta = {
        "run_id": run_id,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "suite": suite,
        "bench": {"name": bench_name,
                  "boards": [{"name": b.name, "model": b.model, "serial": b.serial} for b in boards]},
        "sources": src_versions,
        "select": suite_select or {},          # suite map (requirement↔test) → requirement-centric report
    }
    (out_root / "run-meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))
    return records
