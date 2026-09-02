"""Canonical results store + derived views: compliance matrix, per-cell findings, and a
consolidated report (template-driven, with embedded evidence).

The report is **requirement-centric**: it says how each requirement stands on each element/board.
When the run came from a suite, the suite's requirement↔test MAP is authoritative (a requirement
passes on a board if and only if every test it maps to passed there); ad-hoc/legacy runs fall back to the
tests' advisory `@register(requirements=…)`. Requirement text + *how to verify* come from the
ingested catalog (`atf.core.requirements`); the bench under test and the versions of the checks
*as loaded by the agent* come from the runner's `run-meta.json`.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from atf.core import requirements as reqmeta
from atf.core.runner import Record

SYMBOL = {"gap": "❌", "error": "💥", "manual": "✋", "partial": "⚠",
          "pass": "✅", "na": "—", "notrun": "·"}

_EVIDENCE_LIMIT = 6000


def _cell(verdicts: set[str]) -> str:
    if not verdicts:
        return "notrun"
    if "gap" in verdicts:
        return "gap"
    if "error" in verdicts:
        return "error"
    if "manual" in verdicts:
        return "manual"
    if "skipped" in verdicts:
        return "partial"          # some coverage missing
    if verdicts <= {"pass"}:
        return "pass"
    if verdicts <= {"na"}:
        return "na"
    return "partial"


def _evidence_text(out_root: Path, ev: str) -> str:
    """Read an evidence file's content (truncated) for inline embedding; '' if none."""
    if not ev:
        return ""
    p = out_root / ev
    if not p.exists():
        return ""
    t = p.read_text(errors="replace")
    return t[:_EVIDENCE_LIMIT] + ("\n…(truncated)…" if len(t) > _EVIDENCE_LIMIT else "")


def _load_meta(out_root: Path) -> dict:
    """Run metadata (bench under test, check-source versions, suite map) written by the runner."""
    p = out_root / "run-meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _ver(src: dict | None) -> str:
    """A source's version label: `abc1234 (main, dirty)` — '' when the source isn't a git repo."""
    if not src or not src.get("commit"):
        return ""
    tail = ", ".join(filter(None, [src.get("ref"), "dirty" if src.get("dirty") else ""]))
    return f"{src['commit']} ({tail})" if tail else src["commit"]


def _rec_view(r: Record, src_map: dict, out_root: Path) -> dict:
    """A record enriched for rendering: capability + the source repo/version it was loaded from."""
    return {
        "board": r.board, "check": r.check,
        "capability": " · ".join(filter(None, [",".join(r.drivers) or "host", ",".join(r.actions)])),
        "source": r.source, "version": _ver(src_map.get(r.source)),
        "verdict": r.verdict, "severity": r.severity, "title": r.title, "detail": r.detail,
        "evidence_path": r.evidence, "evidence_text": _evidence_text(out_root, r.evidence),
    }


def _requirement_blocks(records: list[Record], out_root: Path, meta: dict) -> tuple[list[str], list[dict], bool]:
    """Build the per-requirement view — `(boards, blocks, mapped)`.

    `mapped` is True when the run's suite carried a requirement↔test map (the authoritative source
    of the requirement's per-board verdict); otherwise the tests' advisory requirements are used.
    Each block: id, catalog text, version, per-board `cells`, `overall`, and the enriched records.
    """
    from atf.core.registry import _is_map, requirement_verdicts

    src_map = {s["name"]: s for s in (meta.get("sources") or [])}
    boards = sorted({r.board for r in records})
    sel = meta.get("select") or {}
    mapped = _is_map(sel)

    def block(rid: str, tids: list[str], cells: dict[str, str], recs: list[Record]) -> dict:
        m = reqmeta.describe(rid)
        return {
            "id": rid, "title": m.get("title", ""), "desc": m.get("desc", ""),
            "verify": m.get("verify", ""), "priority": m.get("priority"),
            "version": reqmeta.requirement_sha(m) if m else "",
            "tests": tids, "cells": cells,
            "overall": _cell(set(cells.values())),
            "records": [_rec_view(r, src_map, out_root) for r in recs],
        }

    blocks: list[dict] = []
    if mapped:
        rv = requirement_verdicts(sel, [asdict(r) for r in records])   # {boards, requirements:[{id,tests,cells}]}
        boards = rv.get("boards") or boards
        for req in rv.get("requirements", []):
            tids = req["tests"]
            recs = [r for r in records if r.check in tids]
            blocks.append(block(req["id"], tids, dict(req["cells"]), recs))
    else:
        for rid in sorted({req for r in records for req in r.requirements}):
            recs = [r for r in records if rid in r.requirements]
            cells = {bd: _cell({r.verdict for r in recs if r.board == bd}) for bd in boards}
            tids = sorted({r.check for r in recs})
            blocks.append(block(rid, tids, cells, recs))
    return boards, blocks, mapped


def _bench_preamble(meta: dict, boards: list[str]) -> list[str]:
    """Header lines presenting the bench used (element/board under test) + the check-source
    versions loaded — so the matrix says *what was tested, where, and at which revision*."""
    bench = meta.get("bench") or {}
    bmap = {b["name"]: b for b in (bench.get("boards") or [])}
    lines: list[str] = []
    if bench.get("name"):
        lines += [f"**Bench:** {bench['name']}", ""]
    if bmap:
        lines += ["| Board | Model | Serial |", "|---|---|---|"]
        for bd in boards:
            b = bmap.get(bd, {})
            lines.append(f"| {bd} | {b.get('model', '') or '—'} | {b.get('serial', '') or '—'} |")
        lines.append("")
    srcs = meta.get("sources") or []
    if srcs:
        lines += ["**Check sources — versions as loaded:**", "",
                  "| Source | Version | Ref |", "|---|---|---|"]
        for s in srcs:
            v = (s.get("commit") or "—") + (" · dirty" if s.get("dirty") else "")
            lines.append(f"| {s['name']} | {v} | {s.get('ref', '') or '—'} |")
        lines.append("")
    return lines


def write(records: list[Record], out_root: Path, select: dict | None = None,
          history_root: Path | None = None) -> dict:
    """Write a run's artifacts (results.json, matrix.md, findings/, report.md, report.html) into
    `out_root`. When `history_root` is given, the append-only `runs.jsonl` history goes there instead
    (so per-run dirs live under `history_root/runs/<id>` while history stays global)."""
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "findings").mkdir(exist_ok=True)
    meta = _load_meta(out_root)
    if select is not None:                       # caller-supplied suite map (e.g. the agent path)
        meta = {**meta, "select": select}

    # 1) this run's canonical results + append to the (possibly global) history
    rows = [asdict(r) for r in records]
    (out_root / "results.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False))
    (history_root or out_root).mkdir(parents=True, exist_ok=True)
    with ((history_root or out_root) / "runs.jsonl").open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    boards, blocks, mapped = _requirement_blocks(records, out_root, meta)

    # 2) matrix: requirement x board (authoritative per the suite map when present)
    lines = ["# Compliance matrix", ""]
    lines += _bench_preamble(meta, boards)
    if mapped:
        lines.append("_Requirement verdicts follow the suite map: a requirement passes on a board "
                     "if and only if every test it maps to passed there._\n")
    lines += ["| Requirement | " + " | ".join(boards) + " |",
              "|" + "---|" * (len(boards) + 1)]
    for blk in blocks:
        req = blk["id"]
        label = f"{req} — {blk['title']}" if blk["title"] else req
        if blk["version"]:
            label += f" `v:{blk['version']}`"
        cells = []
        for bd in boards:
            st = blk["cells"].get(bd, "notrun")
            cells.append(f"[{SYMBOL[st]}](findings/{req.replace(':', '-')}-{bd}.md)")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines += ["", "Legend: " + "  ".join(f"{s}={k}" for k, s in SYMBOL.items())]
    (out_root / "matrix.md").write_text("\n".join(lines) + "\n")

    # 3) findings per (requirement, board) + 4) consolidated report (markdown + standalone HTML)
    _write_findings(boards, blocks, out_root, meta)
    ctx = build_context(records, boards, blocks, meta)
    (out_root / "report.md").write_text(render_md(ctx))
    (out_root / "report.html").write_text(render_html(ctx))

    counts: dict[str, int] = {}
    for r in records:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    return counts


def _write_findings(boards: list[str], blocks: list[dict], out_root: Path, meta: dict) -> None:
    suite = meta.get("suite", "")
    bench = (meta.get("bench") or {}).get("name", "")
    for blk in blocks:
        req = blk["id"]
        by_board = {bd: [r for r in blk["records"] if r["board"] == bd] for bd in boards}
        for bd in boards:
            recs = by_board[bd]
            head = f"{req} — {blk['title']}" if blk["title"] else req
            status = blk["cells"].get(bd, "notrun")
            lines = [f"# {head} — {bd}", "",
                     f"*Requirement:* {req}"
                     + (f" `v:{blk['version']}`" if blk["version"] else "")
                     + f"  ·  *Board:* {bd}  ·  *Status:* {SYMBOL[status]} {status}"
                     + (f"  ·  *Suite:* {suite}" if suite else "")
                     + (f"  ·  *Bench:* {bench}" if bench else ""), ""]
            if blk.get("desc"):
                lines += [f"**Requirement.** {blk['desc']}", ""]
            if blk.get("verify"):
                lines += [f"**How to verify.** {blk['verify']}", ""]
            if recs:
                lines += ["| Check | Capability | Source · version | Verdict | Severity | Title | Evidence |",
                          "|---|---|---|---|---|---|---|"]
                for r in recs:
                    prov = " · ".join(filter(None, [r["source"], r["version"]])) or "—"
                    lines.append(f"| {r['check']} | {r['capability']} | {prov} | {r['verdict']} "
                                 f"| {r['severity']} | {r['title']} | {r['evidence_path']} |")
                for r in recs:
                    if r["evidence_text"]:
                        lines += ["", f"<details><summary>Evidence — {r['check']} "
                                  f"(<code>{r['evidence_path']}</code>)</summary>", "", "```",
                                  r["evidence_text"], "```", "</details>"]
            else:
                tids = ", ".join(blk.get("tests") or []) or "—"
                lines += [f"_No test result on this board — mapped test(s) ({tids}) did not run here._", ""]
            path = out_root / "findings" / f"{req.replace(':', '-')}-{bd}.md"
            lines += ["", _preserved_notes(path)]        # keep human notes across regeneration
            path.write_text("\n".join(lines) + "\n")


_DEFAULT_NOTES = ("## Analyst notes\n"
                  "- Preconditions:\n- Reproduction:\n- Impact:\n- Remediation:")


def _preserved_notes(path: Path) -> str:
    """The generated header/table refresh every run, but the `## Analyst notes` section is
    human-authored — carry it over from the existing file so it is never clobbered."""
    if path.exists():
        txt = path.read_text()
        i = txt.find("## Analyst notes")
        if i != -1:
            return txt[i:].rstrip("\n")
    return _DEFAULT_NOTES


def build_context(records: list[Record], boards: list[str], blocks: list[dict], meta: dict) -> dict:
    """The shared, render-agnostic report model (used by the markdown + HTML renderers). A run's
    result is presented requirement-first: each requirement carries its catalog text, overall
    verdict, and the tests that determined it."""
    counts: dict[str, int] = {}
    for r in records:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    bench = meta.get("bench") or {}
    return {
        "suite": meta.get("suite") or next((r.suite for r in records if r.suite), ""),
        "bench": bench.get("name") or next((r.bench for r in records if r.bench), ""),
        "boards": boards,
        "bench_boards": bench.get("boards") or [],
        "sources": meta.get("sources") or [],
        "mapped": bool(meta.get("select") and blocks and "tests" in blocks[0]),
        "run_id": meta.get("run_id") or (records[0].run_id if records else ""),
        "generated": meta.get("generated") or datetime.now().isoformat(timespec="seconds"),
        "run": meta.get("run") or {},          # provenance: who ran it, when, agent/AI, framework/env
        "summary": counts,
        "requirements": blocks,
    }


def context_from_records(records: list[Record], meta: dict) -> dict:
    """Build a render context straight from records + a run-meta dict — for callers (the web export)
    that don't go through `write()`. Evidence text is embedded from `meta['evidence_root']` if set."""
    out_root = Path(meta.get("evidence_root") or ".")
    boards, blocks, _ = _requirement_blocks(records, out_root, meta)
    return build_context(records, boards, blocks, meta)


# ------------------------- renderers (pure Python; no template engine) -------------------------
_VERDICT_ORDER = ("gap", "error", "manual", "partial", "notrun", "na", "pass")


def render_md(ctx: dict) -> str:
    """Requirement-first markdown: run header, elements under test, source versions, then one section
    per requirement (title · overall · description · how-to-verify · its tests with detail+verdict)."""
    run = ctx.get("run") or {}
    L = [f"# Test report — {ctx['suite'] or 'ad-hoc run'}", "",
         f"- **Bench:** {ctx['bench'] or '—'}",
         f"- **Run:** `{ctx['run_id']}` · {run.get('at') or ctx['generated']}",
         "- **Verdicts:** " + (" · ".join(f"{v}={n}" for v, n in ctx["summary"].items()) or "—"), ""]
    if run:
        fw = run.get("framework") or {}
        env = run.get("environment") or {}
        via = run.get("ran_via_agent")
        L += ["## Run details", "",
              f"- **Ran by:** {run.get('by') or '—'}",
              f"- **When:** {run.get('at') or ctx['generated']}"]
        if run.get("mgmt_backend"):
            L.append(f"- **Mgmt backend:** {run['mgmt_backend']}")
        if via:
            ai = via.get("ai") or {}
            ai_txt = (f"on ({ai.get('model') or 'default model'}"
                      f"{'' if ai.get('claude') else ', no claude CLI'})") if ai.get("on") else "off"
            L.append(f"- **Ran via agent:** {via.get('name')} `id:{via.get('id')}` "
                     f"(owner {via.get('owner') or '—'}) · AI {ai_txt}")
        else:
            L.append("- **Ran via agent:** — (checks ran on the server)")
        conn = run.get("agents_connected") or []
        if conn:
            L.append("- **Your agents connected at run time:** " +
                     ", ".join(f"{a.get('name')} `id:{a.get('id')}`"
                               f"{' · AI on' if (a.get('ai') or {}).get('on') else ''}" for a in conn))
        fwv = (fw.get("commit") or "—") + (" · dirty" if fw.get("dirty") else "")
        L.append(f"- **Framework:** {fwv}" + (f" ({fw.get('ref')})" if fw.get("ref") else ""))
        if env:
            L.append(f"- **Environment:** python {env.get('python') or '—'} · {env.get('platform') or '—'}"
                     + (f" · host {env.get('host')}" if env.get("host") else ""))
        L.append("")
    if ctx["bench_boards"]:
        L += ["## Element(s) under test", "", "| Board | Model | Serial |", "|---|---|---|"]
        L += [f"| {b['name']} | {b.get('model') or '—'} | {b.get('serial') or '—'} |" for b in ctx["bench_boards"]]
        L.append("")
    if ctx["sources"]:
        L += ["## Check sources — versions as loaded", "", "| Source | Version | Ref |", "|---|---|---|"]
        for s in ctx["sources"]:
            L.append(f"| {s['name']} | {(s.get('commit') or '—')}{' · dirty' if s.get('dirty') else ''} "
                     f"| {s.get('ref') or '—'} |")
        L.append("")
    L.append("## Requirements")
    if ctx["mapped"]:
        L += ["", "_A requirement passes on a board if and only if every test the suite maps to it passed there._"]
    for req in ctx["requirements"]:
        L += ["", f"### {req['id']}" + (f" — {req['title']}" if req["title"] else ""), ""]
        tags = [f"**Overall:** {SYMBOL[req['overall']]} {req['overall']}"]
        if req["version"]:
            tags.append(f"version `{req['version']}`")
        if req["priority"] is not None:
            tags.append(f"priority {req['priority']}")
        L += [" · ".join(tags), ""]
        if len(ctx["boards"]) > 1:            # single board → the overall badge already says it
            L.append("Status per board: " + " · ".join(
                f"{bd}={SYMBOL[req['cells'].get(bd, 'notrun')]} {req['cells'].get(bd, 'notrun')}"
                for bd in ctx["boards"]))
        if req.get("desc"):
            L += ["", f"**Requirement.** {req['desc']}"]
        if req.get("verify"):
            L += ["", f"**How to verify.** {req['verify']}"]
        L += ["", "| Board | Test | Capability | Source · version | Severity | Verdict | Detail |",
              "|---|---|---|---|---|---|---|"]
        for r in req["records"]:
            prov = " · ".join(filter(None, [r["source"], r["version"]])) or "—"
            det = (r.get("detail") or r["title"] or "").replace("|", "\\|").replace("\n", " ")[:300]
            L.append(f"| {r['board']} | {r['check']} | {r['capability']} | {prov} | {r['severity']} "
                     f"| {SYMBOL.get(r['verdict'], '')} {r['verdict']} | {det} |")
        for r in req["records"]:
            if r["evidence_text"]:
                L += ["", f"<details><summary>Evidence — {r['check']} · {r['board']} "
                      f"(<code>{r['evidence_path']}</code>)</summary>", "", "```", r["evidence_text"], "```",
                      "</details>"]
    L += ["", "---", "*Generated by atf. Verdicts: pass=verified · gap=finding · manual=awaiting "
          "sign-off · error=could not determine · partial/skipped=driver or action unavailable.*"]
    return "\n".join(L) + "\n"


def _h(s) -> str:
    import html
    return html.escape(str(s if s is not None else ""), quote=True)


_HTML_CSS = """
:root{--bg:#fff;--fg:#1b1f24;--mut:#6b7280;--line:#e5e7eb;--card:#f9fafb;
 --pass:#15803d;--gap:#b91c1c;--error:#b45309;--manual:#6d28d9;--partial:#a16207;--na:#6b7280;--notrun:#9ca3af;}
@media(prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mut:#9198a1;--line:#30363d;--card:#161b22;
 --pass:#3fb950;--gap:#f85149;--error:#d29922;--manual:#bc8cff;--partial:#e3b341;--na:#8b949e;--notrun:#6e7681;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
 font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1000px;margin:0 auto;padding:32px 22px 80px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:15px;text-transform:uppercase;letter-spacing:.04em;
 color:var(--mut);margin:34px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
h3{font-size:18px;margin:0}.sub{color:var(--mut);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13.5px;margin:8px 0}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:var(--card);font-weight:600}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px}
.req{border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:14px 0;background:var(--card)}
.req .head{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.meta{color:var(--mut);font-size:13px;margin:6px 0 2px}
.block{margin:10px 0}.block .lbl{font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em;color:var(--mut)}
.badge{display:inline-block;padding:1px 9px;border-radius:20px;font-size:12px;font-weight:600;color:#fff;white-space:nowrap}
.b-pass{background:var(--pass)}.b-gap{background:var(--gap)}.b-error{background:var(--error)}
.b-manual{background:var(--manual)}.b-partial{background:var(--partial)}.b-na{background:var(--na)}.b-notrun{background:var(--notrun)}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 2px}
details{margin:6px 0}summary{cursor:pointer;color:var(--mut);font-size:13px}
pre{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;font-size:12px;max-height:340px}
.head-grid{display:flex;gap:22px;flex-wrap:wrap;color:var(--mut);font-size:13.5px;margin:6px 0 2px}
"""


def _badge(state: str) -> str:
    return f'<span class="badge b-{state}">{_h(state)}</span>'


def render_html(ctx: dict) -> str:
    """A self-contained, styled HTML report (no external assets) — requirement-first, print-friendly,
    light/dark aware. Each requirement is a card: title · overall · description · how-to-verify · its
    tests (detail + verdict) with collapsible evidence."""
    P: list[str] = []
    P.append(f'<div class="wrap"><h1>Test report — {_h(ctx["suite"] or "ad-hoc run")}</h1>')
    P.append('<div class="head-grid">'
             + f'<span>Bench <strong>{_h(ctx["bench"] or "—")}</strong></span>'
             + f'<span>Run <code>{_h(ctx["run_id"])}</code></span>'
             + f'<span>{_h(ctx["generated"])}</span></div>')
    P.append('<div class="chips">' + "".join(
        f'{_badge(v)}<span class="sub" style="margin:0 10px 0 3px">{n}</span>'
        for v, n in ctx["summary"].items()) + "</div>")

    run = ctx.get("run") or {}
    if run:
        fw = run.get("framework") or {}
        env = run.get("environment") or {}
        via = run.get("ran_via_agent")
        rows = [("Ran by", _h(run.get("by") or "—")),
                ("When", _h(run.get("at") or ctx["generated"]))]
        if run.get("mgmt_backend"):
            rows.append(("Mgmt backend", _h(run["mgmt_backend"])))
        if via:
            ai = via.get("ai") or {}
            ai_txt = (f'on · {_h(ai.get("model") or "default model")}'
                      + ("" if ai.get("claude") else ' · <span class="badge b-gap">no claude CLI</span>')) \
                if ai.get("on") else "off"
            rows.append(("Ran via agent",
                         f'{_h(via.get("name"))} <code>id:{_h(via.get("id"))}</code> '
                         f'<span class="sub">owner {_h(via.get("owner") or "—")}</span> · AI {ai_txt}'))
        else:
            rows.append(("Ran via agent", '<span class="sub">— (checks ran on the server)</span>'))
        conn = run.get("agents_connected") or []
        if conn:
            rows.append(("Agents connected", ", ".join(
                f'{_h(a.get("name"))} <code>id:{_h(a.get("id"))}</code>'
                + (' <span class="badge b-pass">AI</span>' if (a.get("ai") or {}).get("on") else "")
                for a in conn)))
        fwv = f'<code>{_h(fw.get("commit") or "—")}</code>' + (' · dirty' if fw.get("dirty") else "") \
            + (f' <span class="sub">{_h(fw.get("ref"))}</span>' if fw.get("ref") else "")
        rows.append(("Framework", fwv))
        if env:
            rows.append(("Environment",
                         f'python {_h(env.get("python") or "—")} · <span class="sub">{_h(env.get("platform") or "—")}</span>'
                         + (f' · host {_h(env.get("host"))}' if env.get("host") else "")))
        P.append("<h2>Run details</h2><table>"
                 + "".join(f"<tr><th style='text-align:left;white-space:nowrap'>{k}</th><td>{v}</td></tr>"
                           for k, v in rows) + "</table>")

    if ctx["bench_boards"]:
        P.append("<h2>Element(s) under test</h2><table><tr><th>Board</th><th>Model</th><th>Serial</th></tr>")
        for b in ctx["bench_boards"]:
            P.append(f"<tr><td>{_h(b['name'])}</td><td>{_h(b.get('model') or '—')}</td>"
                     f"<td>{_h(b.get('serial') or '—')}</td></tr>")
        P.append("</table>")
    if ctx["sources"]:
        P.append("<h2>Check sources · versions as loaded</h2><table><tr><th>Source</th><th>Version</th><th>Ref</th></tr>")
        for s in ctx["sources"]:
            dirty = ' <span class="badge b-gap">dirty</span>' if s.get("dirty") else ""
            P.append(f"<tr><td>{_h(s['name'])}</td><td><code>{_h(s.get('commit') or '—')}</code>{dirty}</td>"
                     f"<td>{_h(s.get('ref') or '—')}</td></tr>")
        P.append("</table>")

    P.append("<h2>Requirements</h2>")
    if ctx["mapped"]:
        P.append('<div class="sub">A requirement passes on a board if and only if every test the suite maps to it '
                 "passed there.</div>")
    if not ctx["requirements"]:
        P.append('<div class="sub">No requirements resolved for this run.</div>')
    for req in ctx["requirements"]:
        P.append('<div class="req"><div class="head">'
                 + f'<h3>{_h(req["id"])}' + (f' — {_h(req["title"])}' if req["title"] else "") + "</h3>"
                 + _badge(req["overall"]) + "</div>")
        m = []
        if req["version"]:
            m.append(f'version <code>{_h(req["version"])}</code>')
        if req["priority"] is not None:
            m.append(f"priority {_h(req['priority'])}")
        if m:
            P.append('<div class="meta">' + " · ".join(m) + "</div>")
        if len(ctx["boards"]) > 1:            # per-board breakdown only adds info beyond the title badge
            P.append('<div class="chips">' + "".join(
                f'<span class="sub">{_h(bd)}</span>&nbsp;{_badge(req["cells"].get(bd, "notrun"))}'
                for bd in ctx["boards"]) + "</div>")
        if req.get("desc"):
            P.append(f'<div class="block"><span class="lbl">Requirement</span><div>{_h(req["desc"])}</div></div>')
        if req.get("verify"):
            P.append(f'<div class="block"><span class="lbl">How to verify</span><div>{_h(req["verify"])}</div></div>')
        P.append("<table><tr><th>Board</th><th>Test</th><th>Capability</th><th>Source · version</th>"
                 "<th>Severity</th><th>Verdict</th><th>Detail</th></tr>")
        for r in req["records"]:
            prov = " · ".join(filter(None, [r["source"], r["version"]])) or "—"
            P.append(f"<tr><td>{_h(r['board'])}</td><td><code>{_h(r['check'])}</code></td>"
                     f"<td>{_h(r['capability'])}</td><td class='sub'>{_h(prov)}</td>"
                     f"<td>{_h(r['severity'])}</td><td>{_badge(r['verdict'])}</td>"
                     f"<td>{_h((r.get('detail') or r['title'] or '')[:400])}</td></tr>")
        P.append("</table>")
        for r in req["records"]:
            if r["evidence_text"]:
                P.append(f"<details><summary>Evidence — {_h(r['check'])} · {_h(r['board'])} "
                         f"(<code>{_h(r['evidence_path'])}</code>)</summary><pre>{_h(r['evidence_text'])}</pre></details>")
        P.append("</div>")                    # close this requirement card (siblings, not nested)
    P.append("</div>")                        # close .wrap
    body = "\n".join(P)
    return (f'<!doctype html><html><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>atf report · {_h(ctx["suite"] or ctx["run_id"])}</title>'
            f"<style>{_HTML_CSS}</style></head><body>{body}</body></html>\n")
