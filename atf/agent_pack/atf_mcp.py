#!/usr/bin/env python3
"""atf MCP server — exposes the atf framework to Claude Code as typed tools, proxying the atf
server's REST API. Stdlib only (no install). Reads config from the environment (written to
`.atf-ai.env` by the agent when AI was turned on):

    ATF_SERVER   the atf server URL           ATF_TOKEN    a user session bearer token
    ATF_AID      this agent's id (for scaffolding)         ATF_SOURCES  os.pathsep-joined repo paths

Speaks JSON-RPC 2.0 over stdio (newline-delimited), MCP `initialize` / `tools/list` / `tools/call`.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def _load_env_file():
    """Fall back to `.atf-ai.env` next to this script (written by the agent) when the vars aren't
    already in the environment — so the server finds its config however Claude Code launches it."""
    f = Path(__file__).resolve().parent / ".atf-ai.env"
    if not f.is_file():
        return
    for line in f.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env_file()
SERVER = os.environ.get("ATF_SERVER", "").rstrip("/")
TOKEN = os.environ.get("ATF_TOKEN", "")
AID = os.environ.get("ATF_AID", "")
SOURCES = [p for p in os.environ.get("ATF_SOURCES", "").split(os.pathsep) if p]


def _api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SERVER + path, data=data, method=method)
    req.add_header("content-type", "application/json")
    if TOKEN:
        req.add_header("authorization", "Bearer " + TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=120) as f:
            return f.read().decode()
    except urllib.error.HTTPError as e:
        return json.dumps({"error": e.code, "detail": e.read().decode()[:500]})
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": "request-failed", "detail": str(e)})


# --- tools: (schema, handler) ---
def _t_catalog(_a):
    return _api("GET", "/api/agents/catalog")


def _t_suites(_a):
    return _api("GET", "/api/suites")


def _t_run(a):
    body = {"bench": a["bench"], "board": a["board"], "mgmt_backend": a.get("mgmt_backend", "local")}
    if a.get("suite"):
        body["suite"] = a["suite"]
    if a.get("ids"):
        body["ids"] = a["ids"]
    return _api("POST", "/api/run", body)


def _t_report(a):
    return _api("GET", "/api/reports/" + a["run_id"])


def _t_scaffold(a):
    if not AID:
        return json.dumps({"error": "no ATF_AID — re-enable AI on the agent"})
    kind = a.get("kind", "auto")
    ep = "manual" if kind == "manual" else "scaffold"
    return _api("POST", f"/api/agents/{AID}/{ep}", {
        "id": a["id"], "source": a["source"], "model": a.get("model", "common"),
        "drivers": a.get("drivers", []), "actions": a.get("actions", []),
        "severity": a.get("severity", "medium"), "title": a.get("title", "")})


def _t_api(a):
    method = (a.get("method") or "GET").upper()
    path = a.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    return _api(method, path, a.get("body"))


def _t_evidence(a):
    """Raw evidence text a check wrote in a run — what the black-box probe actually saw. `path` is a
    record's `evidence` field (e.g. 'evidence/mgmt-tls-b1.txt'); `run_id` from the report."""
    qs = urllib.parse.urlencode({"path": a["path"], "run_id": a.get("run_id", "")})
    return _api("GET", "/api/evidence?" + qs)


def _t_benches(a):
    """List benches to run against — or one bench's boards (pass `name`), so you know the bench/board
    to give atf_run."""
    name = (a.get("name") or "").strip()
    return _api("GET", "/api/benches/" + urllib.parse.quote(name) if name else "/api/benches")


def _t_requirements(a):
    """The requirement catalogs (frameworks) or, with `framework`, the requirements in one — the
    left-hand side of the suite map."""
    fw = (a.get("framework") or "").strip()
    if fw:
        return _api("GET", "/api/requirements?framework=" + urllib.parse.quote(fw))
    return _api("GET", "/api/requirements/frameworks")


def _t_map(a):
    """Save a suite's requirement→test map, then validate it. `requirements` is an ORDERED list of
    {id, tests:[test-id,…], fallback}: run order = list order (setup→…→teardown); a requirement
    passes iff every mapped test passed; no test → the fallback (TEST_PASS/TEST_FAIL) decides."""
    reqs = a.get("requirements") or []
    select = {"model": a.get("model", "") or "",
              "requirements": [{"id": r["id"], "fallback": r.get("fallback", "TEST_FAIL"),
                                "tests": [{"id": t} for t in (r.get("tests") or [])]}
                               for r in reqs]}
    put = _api("PUT", "/api/suites/" + urllib.parse.quote(a["name"]),
               {"title": a.get("title", ""), "description": a.get("description", ""), "select": select})
    validation = _api("POST", "/api/suites/validate", {"select": select})
    return json.dumps({"saved": _loads(put), "validation": _loads(validation)}, ensure_ascii=False)


def _t_testplan(a):
    """List/get/save/run a Test Plan — a named Suite + bench/board. `op`: list | get | save | run.
    'run' resolves the plan and starts it (fetch the report with atf_report)."""
    op = (a.get("op") or "list").lower()
    name = urllib.parse.quote(a.get("name", ""))
    if op == "list":
        return _api("GET", "/api/test-plans")
    if op == "get":
        return _api("GET", "/api/test-plans/" + name)
    if op == "save":
        return _api("PUT", "/api/test-plans/" + name, {
            "suite": a.get("suite", ""), "bench": a.get("bench", ""),
            "board": a.get("board", ""), "mgmt_backend": a.get("mgmt_backend", "docker")})
    if op == "run":
        plan = _loads(_api("GET", "/api/test-plans/" + name))
        if not isinstance(plan, dict) or plan.get("error"):
            return json.dumps({"error": "plan not found", "detail": plan})
        board = plan.get("board")
        return _api("POST", "/api/run", {
            "suite": plan.get("suite"), "bench": plan.get("bench"),
            "board": [board] if isinstance(board, str) else (board or []),
            "mgmt_backend": plan.get("mgmt_backend", "docker")})
    return json.dumps({"error": "unknown op", "op": op})


def _loads(s):
    try:
        return json.loads(s)
    except Exception:
        return s


TOOLS = [
    ({"name": "atf_catalog", "description": "List all tests the framework knows (id, drivers, actions, mode, model) from the server + connected agents.",
      "inputSchema": {"type": "object", "properties": {}}}, _t_catalog),
    ({"name": "atf_suites", "description": "List saved suites (the requirement→test maps).",
      "inputSchema": {"type": "object", "properties": {}}}, _t_suites),
    ({"name": "atf_run", "description": "Run tests against a bench/board — a saved suite, or ad-hoc by test ids. Returns the run acknowledgement; fetch the report with atf_report.",
      "inputSchema": {"type": "object", "properties": {
          "bench": {"type": "string"}, "board": {"type": "array", "items": {"type": "string"}},
          "suite": {"type": "string"}, "ids": {"type": "array", "items": {"type": "string"}},
          "mgmt_backend": {"type": "string", "enum": ["local", "docker"]}},
          "required": ["bench", "board"]}}, _t_run),
    ({"name": "atf_report", "description": "Fetch a run's report: per-test records (verdict, drivers, actions, skip reasons) + requirement×board roll-up.",
      "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]}}, _t_report),
    ({"name": "atf_scaffold", "description": "Scaffold a new test file on this agent's repo. kind=auto → a .py driver test; kind=manual → a Markdown .md. drivers/actions are the framework capabilities it declares.",
      "inputSchema": {"type": "object", "properties": {
          "kind": {"type": "string", "enum": ["auto", "manual"]}, "id": {"type": "string"},
          "source": {"type": "string", "description": "a repo the agent serves"}, "model": {"type": "string"},
          "drivers": {"type": "array", "items": {"type": "string"}}, "actions": {"type": "array", "items": {"type": "string"}},
          "severity": {"type": "string"}, "title": {"type": "string"}},
          "required": ["id", "source"]}}, _t_scaffold),
    ({"name": "atf_evidence", "description": "Fetch the RAW evidence text a check wrote in a run (what the black-box probe actually saw) — for interpreting a gap or debugging. `path` is a record's `evidence` field from atf_report; `run_id` is the run.",
      "inputSchema": {"type": "object", "properties": {
          "run_id": {"type": "string"}, "path": {"type": "string", "description": "a record's evidence field, e.g. evidence/mgmt-tls-b1.txt"}},
          "required": ["path"]}}, _t_evidence),
    ({"name": "atf_benches", "description": "List benches to run against; pass `name` for one bench's boards. Use it to pick the bench + board for atf_run.",
      "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}}}, _t_benches),
    ({"name": "atf_requirements", "description": "List requirement catalogs (frameworks); pass `framework` for the requirements in one. The left-hand side of a suite map.",
      "inputSchema": {"type": "object", "properties": {"framework": {"type": "string"}}}}, _t_requirements),
    ({"name": "atf_map", "description": "Save a suite's requirement→test map and validate it. `requirements` is an ORDERED list of {id, tests:[test-id…], fallback} — run order = list order (setup→…→teardown); a requirement passes iff every mapped test passed; with no test the fallback (TEST_PASS/TEST_FAIL) decides. Maps by test id (resolved at run time by the running user's own agent).",
      "inputSchema": {"type": "object", "properties": {
          "name": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
          "model": {"type": "string", "description": "'' = any board, or a model slug"},
          "requirements": {"type": "array", "items": {"type": "object", "properties": {
              "id": {"type": "string"}, "tests": {"type": "array", "items": {"type": "string"}},
              "fallback": {"type": "string", "enum": ["TEST_PASS", "TEST_FAIL"]}}, "required": ["id"]}}},
          "required": ["name", "requirements"]}}, _t_map),
    ({"name": "atf_testplan", "description": "List/get/save/run a Test Plan (a named Suite + bench/board). op: list | get | save | run. 'run' resolves the plan and starts it — fetch results with atf_report.",
      "inputSchema": {"type": "object", "properties": {
          "op": {"type": "string", "enum": ["list", "get", "save", "run"], "default": "list"},
          "name": {"type": "string"}, "suite": {"type": "string"}, "bench": {"type": "string"},
          "board": {"type": "string"}, "mgmt_backend": {"type": "string", "enum": ["local", "docker"]}}}}, _t_testplan),
    ({"name": "atf_api", "description": "Call ANY atf REST API endpoint — the general-purpose tool for anything the curated tools don't cover (inventory, benches, requirements, test-plans, board-models, reports list, suites CRUD/validate/export, …). Permissions are enforced by your session token: admin-only endpoints are denied. Examples: GET /api/inventory/boards, GET /api/benches, GET /api/requirements, GET /api/test-plans, GET /api/reports, PUT /api/suites/{name}. Prefer the specific tools (atf_catalog/atf_run/atf_report/…) for common flows.",
      "inputSchema": {"type": "object", "properties": {
          "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"], "default": "GET"},
          "path": {"type": "string", "description": "e.g. /api/inventory/boards"},
          "body": {"type": "object", "description": "JSON body for POST/PUT"}},
          "required": ["path"]}}, _t_api),
]
_BY_NAME = {t[0]["name"]: t for t in TOOLS}


def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _result(id_, result):
    _send({"jsonrpc": "2.0", "id": id_, "result": result})


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            _result(mid, {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "atf", "version": "1.0.0"}})
        elif method == "tools/list":
            _result(mid, {"tools": [t[0] for t in TOOLS]})
        elif method == "tools/call":
            p = msg.get("params") or {}
            entry = _BY_NAME.get(p.get("name"))
            if not entry:
                _result(mid, {"content": [{"type": "text", "text": "unknown tool"}], "isError": True})
            else:
                try:
                    text = entry[1](p.get("arguments") or {})
                except Exception as e:  # noqa: BLE001
                    text, err = f"tool error: {e}", True
                else:
                    err = False
                _result(mid, {"content": [{"type": "text", "text": text}], "isError": err})
        elif mid is not None:                         # unknown request → empty result (keep the client happy)
            _result(mid, {})
        # notifications (no id) get no response


if __name__ == "__main__":
    main()
