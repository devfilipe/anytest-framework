"""`atf` CLI: run / list / report."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

from atf.core import inventory, report, runner
from atf.core.checks import discover as _discover_checks
from atf.core.registry import REGISTRY, select

_discover_checks()  # import all checks from the configured check-source repos (registers them)


def _csv(v: Optional[str]) -> Optional[list[str]]:
    return [x.strip() for x in v.split(",") if x.strip()] if v else None


def _open_repo_opt():
    """Config store, or None if it can't be opened — lets file-based runs work without a store."""
    try:
        from atf.store import open_repo
        return open_repo()
    except Exception:
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="atf", description="Anytest Framework")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run selected checks against the bench")
    p.add_argument("--bench", default="benches/lab.yaml",
                   help="bench: a store bench name, or an inventory YAML file (default: benches/lab.yaml)")
    p.add_argument("--secrets", default=None,
                   help="secrets for *_ref (default: benches/<name>.secrets.yaml)")
    p.add_argument("--out", default="reports", help="output dir (results.json/matrix/findings)")
    p.add_argument("--req", help="comma list of requirement ids (e.g. C.4,G.2 or acme:C.4)")
    p.add_argument("--vector", help="comma list of vectors (console,craft,mgmt)")
    p.add_argument("--board", help="comma list of board names")
    p.add_argument("--id", help="comma list of check ids")
    p.add_argument("--suite", help="named suite: a store suite, or suites/<name>.yaml (see: atf suites)")
    p.add_argument("--mgmt-backend", choices=["docker", "local"], default="docker",
                   help="how to run mgmt checks (default: atf-mgmt container)")
    p.add_argument("--mgmt-image", default="atf-mgmt:latest", help="atf-mgmt image tag")

    sub.add_parser("list", help="list registered checks")
    sub.add_parser("suites", help="list available suites (config store + suites/*.yaml)")

    pr = sub.add_parser("report", help="regenerate matrix/findings from results.json")
    pr.add_argument("--out", default="reports", help="output dir (default: reports)")

    nc = sub.add_parser("new-check", help="scaffold a new check module + register it")
    nc.add_argument("--id", required=True, help="check id (neutral, e.g. mgmt-tls-enum)")
    nc.add_argument("--vector", default="host",
                    help="host|console|craft|mgmt (or a new vector name)")
    nc.add_argument("--req", help="comma requirement ids, namespaced (e.g. acme:E.3)")
    nc.add_argument("--severity", default="medium",
                    choices=["info", "low", "medium", "high", "critical"])
    nc.add_argument("--mode", default="auto", choices=["auto", "manual"],
                    help="auto = programmatic; manual = operator-driven (atf.core.manual)")
    nc.add_argument("--model", default="common",
                    help="common (any board) or a model slug, e.g. router_x_lite")
    nc.add_argument("--title", default="", help="human title (defaults to the id)")

    wb = sub.add_parser("web", help="dashboard over reports/ + pilot runs (localhost)")
    wb.add_argument("--out", default="reports", help="store dir to serve (default: reports)")
    wb.add_argument("--bench", default="benches/lab.yaml", help="inventory to pilot runs against")
    wb.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    wb.add_argument("--port", type=int, default=8899, help="port (default: 8899)")

    st = sub.add_parser("store", help="config store (SQLite): list/import/export benches & suites")
    st.add_argument("action", choices=["list", "import", "export"])
    st.add_argument("kind", nargs="?", choices=["bench", "suite"], help="for import/export")
    st.add_argument("name", nargs="?", help="bench/suite name")
    st.add_argument("--file", help="YAML file (import reads it; export writes it, default stdout)")

    bk = sub.add_parser("backup", help="write a consistent snapshot of the whole config store")
    bk.add_argument("--out", default="atf-backup.db", help="snapshot file path (default: atf-backup.db)")

    rs = sub.add_parser("restore", help="replace the config store from a snapshot (disruptive)")
    rs.add_argument("file", help="snapshot file produced by `atf backup`")
    rs.add_argument("--yes", action="store_true", help="skip the confirmation prompt")

    w = sub.add_parser("_mgmt-worker", help="(internal) worker run inside the atf-mgmt container")
    w.add_argument("--out", default="/out")

    ag = sub.add_parser("agent", help="run the dev/host agent (connect out to a atf server)")
    ag.add_argument("--server", required=True, help="atf server URL, e.g. http://192.0.2.10:8899")
    ag.add_argument("--token", required=True, help="agent enrollment token (Admin › Agents)")
    ag.add_argument("--source", action="append", default=[],
                    help="local check-source repo (repeatable), e.g. ~/src/anytest-checks-router-x")
    ag.add_argument("--name", default="", help="agent name (default: hostname)")

    sub.add_parser("_agent-worker", help="(internal) isolated run of a pushed working tree")

    args = ap.parse_args(argv)

    if args.cmd == "_mgmt-worker":
        import sys
        from atf.access.mgmt import worker as mgmt_worker
        request = json.load(sys.stdin)
        print(json.dumps(mgmt_worker.run(request, Path(args.out))))
        return 0

    if args.cmd == "agent":
        from atf import agent
        if not args.source:
            print("error: pass at least one --source <repo dir>")
            return 2
        return agent.run(args.server, args.token, args.source, args.name)

    if args.cmd == "_agent-worker":
        import dataclasses
        import sys
        req = json.load(sys.stdin)
        sel = req.get("select") or {}
        # a suite selection (req/include/exclude/model) resolves via resolve_selection — identical to
        # the server pilot; a bare {ids}/{vectors} ad-hoc run keeps the plain filter
        if sel.get("req") or sel.get("requirements") or sel.get("include") or sel.get("exclude") or sel.get("model"):
            from atf.core.registry import resolve_selection
            specs = resolve_selection(sel)
        else:
            specs = select(ids=sel.get("ids"), requirements=sel.get("req"), drivers=sel.get("vectors"))
        name = req.get("bench") or ""
        try:
            from atf.store import open_repo
            repo = open_repo()
            bench = repo.inventory_bench(name) if name and repo.get_bench(name) else \
                inventory.load(req.get("bench_path", "benches/lab.yaml"))
        except Exception:
            bench = inventory.load(req.get("bench_path", "benches/lab.yaml"))
        _b = req.get("board")
        boards = (set(_b) if isinstance(_b, list) else {_b}) if _b else None
        recs = runner.run(bench, specs, Path(req["out"]), boards_filter=boards,
                          mgmt_backend=req.get("mgmt_backend", "local"),
                          suite=req.get("suite", ""), bench_name=name, suite_select=sel)
        payload = json.dumps([dataclasses.asdict(r) for r in recs])
        # hand results back via a file — robust against any stdout noise a check may print
        (Path(req["out"]) / "records.json").write_text(payload)
        print(payload)
        return 0

    if args.cmd == "list":
        for spec in REGISTRY.values():
            vec = ",".join(sorted(spec.drivers)) or "host"
            tag = f"[{spec.mode}]" + ("[disruptive]" if spec.disruptive else "")
            print(f"{spec.id:20} vec={vec:8} req={','.join(spec.requirements):22} "
                  f"{tag} {spec.title}")
        return 0

    if args.cmd == "suites":
        from atf.core import suite
        seen = set()
        repo = _open_repo_opt()
        if repo:
            for s in repo.list_suites():
                print(f"{s['name']:16} {s.get('title', ''):32} [store]")
                seen.add(s["name"])
        for name, title in suite.available():
            if name in seen:
                continue
            print(f"{name:16} {title:32} [file]")
        return 0

    if args.cmd == "new-check":
        from atf.core import scaffold
        try:
            path = scaffold.new_check(id=args.id, driver=args.vector, severity=args.severity,
                                      title=args.title, model=args.model)
        except (ValueError, FileExistsError) as e:
            print(f"error: {e}")
            return 2
        print(f"created {path}")
        print("discovered by atf_checks (walk) — edit the TODOs, then: atf list")
        if args.vector == "mgmt":
            print("mgmt check: rebuild the container before running it → make image")
        return 0

    if args.cmd == "web":
        from atf.web.server import serve
        return serve(Path(args.out), bench_path=args.bench, host=args.host, port=args.port)

    if args.cmd == "backup":
        from atf.store import open_repo
        p = open_repo().backup(args.out)
        print(f"backup written: {p}  (restore with: atf restore {p})")
        print("note: secrets stay encrypted with APP_SECRET — back that up too.")
        return 0

    if args.cmd == "restore":
        from atf.store import open_repo
        if not args.yes:
            resp = input(f"Replace the current config store with {args.file}? [y/N] ")
            if resp.strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        open_repo().restore(args.file)
        print(f"restored config store from {args.file}")
        return 0

    if args.cmd == "store":
        import yaml
        from atf.store import open_repo
        repo = open_repo()
        if args.action == "list":
            print("benches:")
            for b in repo.list_benches():
                print(f"  {b['name']:16} agents={b['agents']} boards={b['boards']} secrets={b['secrets']}")
            print("suites:")
            for s in repo.list_suites():
                print(f"  {s['name']:16} {s['title']}")
            return 0
        if not args.kind or not args.name:
            print("error: need <bench|suite> <name>")
            return 2
        if args.action == "export":
            data = repo.get_bench(args.name) if args.kind == "bench" else repo.get_suite(args.name)
            if data is None:
                print(f"not found: {args.kind} {args.name}")
                return 1
            out = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
            if args.file:
                Path(args.file).write_text(out)
                print("wrote", args.file)
            else:
                print(out)
            return 0
        # import
        if not args.file:
            print("error: import needs --file")
            return 2
        data = yaml.safe_load(Path(args.file).read_text()) or {}
        if args.kind == "bench":
            repo.upsert_bench(args.name, data)
            sec = Path(args.file).with_name(Path(args.file).stem + ".secrets.yaml")
            if sec.exists():
                repo.set_secrets(args.name, yaml.safe_load(sec.read_text()) or {})
                print("imported secrets from", sec.name)
        else:
            repo.upsert_suite(args.name, data)
        print("imported", args.kind, args.name)
        return 0

    if args.cmd == "report":
        rows = json.loads((Path(args.out) / "results.json").read_text())
        recs = [runner.Record(**r) for r in rows]
        print("regenerated:", report.write(recs, Path(args.out)))
        return 0

    # run — a suite provides the default selection; explicit flags override it.
    # bench/suite names resolve from the config store when they aren't a local file (parity with the web).
    req, vec, ids = _csv(args.req), _csv(args.vector), _csv(args.id)
    sel = None                                          # the suite's raw `select` (file or store)
    if args.suite:
        fp = Path("suites", f"{args.suite}.yaml")
        if fp.exists():
            import yaml
            sel = (yaml.safe_load(fp.read_text()) or {}).get("select") or {}
        else:
            repo = _open_repo_opt()
            s = repo.get_suite(args.suite) if repo else None
            if s is None:
                print(f"error: suite not found (file or store): {args.suite}")
                return 2
            sel = s.get("select") or {}
    try:
        # a suite with no explicit-flag override resolves via resolve_selection — it handles every
        # select shape (requirement↔test map, legacy req/include/exclude, or a bare ids list), the
        # same way the web pilot and agent worker do. Explicit flags force the ad-hoc filter.
        if sel is not None and not (req or vec or ids):
            from atf.core.registry import resolve_selection
            specs = resolve_selection(sel)
        else:
            base = sel or {}
            specs = select(requirements=req or base.get("req"),
                           drivers=vec or base.get("vectors"),
                           ids=ids or base.get("ids"))
    except ValueError as e:
        print(f"error: {e}")
        return 2
    if not specs:
        print("no checks selected")
        return 1
    # bench: a local YAML file, else a store bench name
    if Path(args.bench).exists():
        bench, bench_name = inventory.load(args.bench, args.secrets), Path(args.bench).stem
    else:
        repo = _open_repo_opt()
        if not (repo and repo.get_bench(args.bench)):
            print(f"error: bench not found (file or store): {args.bench}")
            return 2
        bench, bench_name = repo.inventory_bench(args.bench), args.bench
    out = Path(args.out)
    boards = set(_csv(args.board)) if args.board else None
    recs = runner.run(bench, specs, out, boards_filter=boards,
                      mgmt_backend=args.mgmt_backend, mgmt_image=args.mgmt_image,
                      suite=args.suite or "", bench_name=bench_name, suite_select=sel)
    counts = report.write(recs, out)
    print(f"\nran {len(recs)} check-result(s) across "
          f"{len({r.board for r in recs})} board(s)")
    print("verdicts:", counts)
    print(f"→ {out}/matrix.md  ·  {out}/results.json  ·  {out}/findings/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
