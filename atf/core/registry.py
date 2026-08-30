"""Check plugin registry: @register decorator + selection."""
from __future__ import annotations

from typing import Callable, Iterable, Optional

from atf.core.model import Ctx, CheckSpec, Result, Severity

REGISTRY: dict[str, CheckSpec] = {}


def model_of_module(module: str) -> str:
    """Infer a check's model from its module path: `atf_checks.<model>.<vector>.<slug>` →
    "" for `common`, else the `<model>` slug. Anything outside `atf_checks` is common."""
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "atf_checks":
        return "" if parts[1] == "common" else parts[1]
    return ""


def register(*, id: str, drivers: Iterable[str] = (), actions: Iterable[str] = (),
             mode: str = "auto", severity: Severity = Severity.MEDIUM, title: str = "",
             disruptive: bool = False, model: Optional[str] = None):
    """Decorate a `fn(ctx) -> Result` to register it as a check. A test declares only the framework
    capabilities it needs: `drivers` (comm channels console/craft/mgmt) and `actions` (node actions
    like power-cycle); the runner gates on the bench providing them. A test does NOT declare
    requirements — the **Suite** owns requirement↔test mapping. `disruptive=True` excludes it from
    suite selection (only runs when named with --id). `model` defaults to the module path."""
    def deco(fn: Callable[[Ctx], Result]):
        if id in REGISTRY:
            raise ValueError(f"duplicate check id: {id}")
        mdl = model if model is not None else model_of_module(getattr(fn, "__module__", ""))
        REGISTRY[id] = CheckSpec(
            id=id, drivers=frozenset(drivers), actions=frozenset(actions),
            mode=mode, severity=severity, title=title or id, fn=fn, disruptive=disruptive, model=mdl)
        return fn
    return deco


def register_md_manual(id: str, *, model: str = "", drivers: Iterable[str] = (),
                       actions: Iterable[str] = (), severity: str = "medium", title: str = "",
                       disruptive: bool = False, body: str = "", path: str = "") -> None:
    """Register a Markdown manual test discovered as a repo file (atf_checks/<model>/manual/<id>.md).
    Like a code check but the artifact IS the Markdown: fn prompts the operator with the body.
    Drivers/actions come from the frontmatter (usually none → always available to the operator).
    Overwrites any prior spec with the same id (discovery re-runs on reload)."""
    from atf.core import manual
    sev = Severity(severity) if severity in Severity._value2member_map_ else Severity.MEDIUM

    def fn(ctx, _b=body, _s=sev):
        return manual.prompt(_b, default_severity=_s, check_id=ctx.check_id)

    REGISTRY[id] = CheckSpec(
        id=id, drivers=frozenset(drivers), actions=frozenset(actions),
        mode="manual", severity=sev, title=title or id, fn=fn, disruptive=bool(disruptive),
        model=model, path=path)


def _split_ns(r: str) -> tuple[str, str]:
    """`acme:G.2` -> ("acme", "G.2"); an un-namespaced id -> ("", id)."""
    ns, sep, suffix = r.partition(":")
    return (ns, suffix) if sep else ("", r)


def _req_hit(check_reqs: Iterable[str], filters: set) -> bool:
    """Requirement IDs are namespaced `framework:id` (e.g. acme:G.2). A filter matches
    the full id OR its bare suffix, so `--req G.2` also selects `acme:G.2`."""
    for r in check_reqs:
        if r in filters or _split_ns(r)[1] in filters:
            return True
    return False


def _ambiguous_bare_filters(filters: set) -> dict[str, set[str]]:
    """A bare (un-namespaced) filter is a cross-namespace alias: `C.4` matches the suffix
    in ANY catalog. Return {bare_filter: {namespaces…}} for every bare filter whose
    suffix is defined in MORE THAN ONE namespace — i.e. the ones that are ambiguous."""
    bare = {f for f in filters if ":" not in f}
    if not bare:
        return {}
    hits: dict[str, set[str]] = {f: set() for f in bare}
    for spec in REGISTRY.values():
        for r in spec.requirements:
            ns, suffix = _split_ns(r)
            if suffix in bare and ns:
                hits[suffix].add(ns)
    return {f: ns for f, ns in hits.items() if len(ns) > 1}


def select(requirements: Optional[Iterable[str]] = None,
           drivers: Optional[Iterable[str]] = None,
           ids: Optional[Iterable[str]] = None,
           model: Optional[str] = None) -> list[CheckSpec]:
    reqs = set(requirements) if requirements else None
    vecs = set(drivers) if drivers else None
    idset = set(ids) if ids else None
    if reqs:
        amb = _ambiguous_bare_filters(reqs)
        if amb:
            detail = "; ".join(
                f"{f!r} → " + ", ".join(sorted(f"{ns}:{f}" for ns in nss))
                for f, nss in sorted(amb.items()))
            raise ValueError(
                f"ambiguous requirement filter, matches multiple catalogs: {detail}. "
                "Qualify it, e.g. --req <framework>:<id>.")
    out = []
    for spec in REGISTRY.values():
        named = bool(idset and spec.id in idset)
        # disruptive checks (reboot/mutate the board) only run when EXPLICITLY named by --id
        if spec.disruptive and not named:
            continue
        if idset and spec.id not in idset:
            continue
        # model gating: common checks (model=="") apply everywhere; model checks only to that model
        if model and spec.model and spec.model != model:
            continue
        if reqs and not _req_hit(spec.requirements, reqs):
            continue
        # host checks (no drivers) always pass a driver filter; driver checks must fit
        if vecs and spec.drivers and not (spec.drivers <= vecs):
            continue
        out.append(spec)
    return out


def _is_map(sel: dict) -> bool:
    """True if `sel` is the new Suite-as-map shape: requirements = list of dicts {id, tests, …}."""
    r = (sel or {}).get("requirements")
    return isinstance(r, list) and bool(r) and isinstance(r[0], dict)


def resolve_selection(sel: Optional[dict]) -> list[CheckSpec]:
    """Resolve a saved suite selection → the CheckSpecs to run, identically wherever a suite runs
    (server pilot or agent worker).

    NEW (Suite-as-map): sel = {model, requirements:[{id, tests:[{id,…}], fallback}]} → run the tests
    mapped to the requirements (model-gated) in the SUITE'S DECLARED ORDER — requirements top-to-bottom,
    tests within a requirement in order, first occurrence wins (so setup/teardown sequencing holds).
    The requirement↔test mapping is the suite's, not derived from @register.

    LEGACY: sel = {model, req, include, exclude} → (checks covering req for the model) ∪ include − exclude.
    Drivers/actions are NOT filtered here — a check whose driver/action the board lacks is reported
    'unavailable' at run time (they are bench properties, not suite ones)."""
    sel = sel or {}
    model = sel.get("model") or None
    if _is_map(sel):
        ordered_ids, seen = [], set()               # preserve suite order, dedup first-wins
        for req in sel["requirements"]:
            for t in (req.get("tests") or []):
                tid = t.get("id")
                if tid and tid not in seen:
                    seen.add(tid)
                    ordered_ids.append(tid)
        if not ordered_ids:
            return []
        by_id = {s.id: s for s in select(ids=ordered_ids, model=model)}
        return [by_id[i] for i in ordered_ids if i in by_id]   # honor the declared order
    reqs = sel.get("req") or sel.get("requirements")
    include = list(sel.get("include") or sel.get("ids") or [])
    exclude = set(sel.get("exclude") or [])
    base = select(requirements=reqs, model=model) if reqs else []
    inc = select(ids=include, model=model) if include else []
    chosen = {s.id: s for s in [*base, *inc] if s.id not in exclude}
    return list(chosen.values())


def requirement_verdicts(sel: Optional[dict], records: list) -> dict:
    """Roll a run's records up to per-requirement × board PASS/FAIL, using the suite MAP.
    BINARY: a requirement PASSES on a board if and only if every mapped test ran and passed there; anything else
    (any non-pass verdict, or a mapped test that didn't run) is FAIL. With no mapped tests the verdict
    is the fallback placeholder (TEST_PASS→pass, TEST_FAIL→gap). Returns {} for legacy (unmapped) suites."""
    sel = sel or {}
    if not _is_map(sel):
        return {}
    boards = sorted({r.get("board", "") for r in records})
    idx: dict = {}                                  # (check, board) -> set of verdicts seen
    for r in records:
        idx.setdefault((r.get("check"), r.get("board", "")), set()).add(r.get("verdict"))
    out = []
    for req in sel["requirements"]:
        tids = [t.get("id") for t in (req.get("tests") or []) if t.get("id")]
        cells = {}
        for b in boards:
            if not tids:
                cells[b] = "pass" if req.get("fallback") == "TEST_PASS" else "gap"
            else:
                cells[b] = "pass" if all(idx.get((tid, b)) == {"pass"} for tid in tids) else "gap"
        out.append({"id": req.get("id"), "tests": tids,
                    "fallback": req.get("fallback") or "TEST_FAIL", "cells": cells})
    return {"boards": boards, "requirements": out}
