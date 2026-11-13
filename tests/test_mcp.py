"""`atf mcp`: the MCP server (JSON-RPC 2.0 over stdio) for a running atf server. Each test starts
the real web app in a local uvicorn (conftest `server`), logs in with `atf remote login`, and talks
to `atf mcp` as a subprocess, the way an MCP client does."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

# routes the MCP does not expose as tools: the session itself (`atf remote login` owns it), the
# agent wire protocol (the agent speaks it, not a person) and the SSE stream (not request/response)
NOT_TOOLS = {("POST", "/api/admin/login"), ("POST", "/api/agents/register"), ("GET", "/api/agents/{aid}/poll"),
             ("POST", "/api/agents/{aid}/tree"), ("POST", "/api/agents/{aid}/inspect"),
             ("POST", "/api/agents/{aid}/catalog"), ("POST", "/api/agents/{aid}/file"),
             ("GET", "/api/run/stream")}


class Mcp:
    def __init__(self, env):
        self.p = subprocess.Popen([sys.executable, "-c", "from atf.cli import main; raise SystemExit(main(['mcp']))"],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, env=env)
        self.n = 0

    def request(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method, "params": params or {}}) + "\n")
        self.p.stdin.flush()
        reply = json.loads(self.p.stdout.readline())
        assert reply["id"] == self.n
        return reply["result"]

    def call(self, tool, **arguments):
        return self.request("tools/call", {"name": tool, "arguments": arguments})

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


@pytest.fixture
def mcp(server, config_dir, monkeypatch):
    import os

    from atf.cli import main
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "admin")
    assert main(["remote", "login", "--server", server, "--user", "admin"]) == 0
    m = Mcp({**os.environ, "ATF_CONFIG_DIR": str(config_dir)})
    m.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    yield m
    m.close()


def _routes(app):
    return {(m, r.path) for r in app.routes if getattr(r, "path", "").startswith("/api/")
            for m in (getattr(r, "methods", None) or ()) if m in ("GET", "POST", "PUT", "DELETE", "PATCH")}


def test_every_api_route_is_an_operation_of_some_tool(mcp, server):
    import urllib.request
    spec = json.load(urllib.request.urlopen(server + "/api/openapi.json"))
    routes = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops}

    tools = mcp.request("tools/list")["tools"]

    offered = {tuple(op.split(" ", 1)) for t in tools if t["name"].startswith("atf_api_")
               for op in t["inputSchema"]["properties"]["op"]["enum"]}
    assert routes - NOT_TOOLS - offered == set()
    assert offered <= routes


def _text(result):
    return result["content"][0]["text"]


def test_a_read_operation_returns_the_server_json(mcp):
    r = mcp.call("atf_api_benches", op="GET /api/benches/{name}", params={"name": "lab"})

    assert r["isError"] is False
    assert json.loads(_text(r))["boards"][0]["drivers"]["mgmt"]["ip"] == "192.0.2.1"


def test_a_write_operation_sends_the_body(mcp, store):
    bench = {"agents": {}, "boards": [{"name": "board-2", "model": "router-x", "serial": "2", "creds": {},
                                        "drivers": {"mgmt": {"driver_name": "ip", "ip": "192.0.2.2"}}}]}

    r = mcp.call("atf_api_benches", op="PUT /api/benches/{name}", params={"name": "lab2"}, body=bench)

    assert r["isError"] is False, _text(r)
    assert store.get_bench("lab2")["boards"][0]["drivers"]["mgmt"]["ip"] == "192.0.2.2"


def test_secrets_go_in_by_file_and_never_come_back(mcp, store, tmp_path):
    pem = "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49\n-----END PRIVATE KEY-----\n"
    (tmp_path / "k.pem").write_text(pem)

    r = mcp.call("atf_api_benches", op="PUT /api/benches/{name}/secrets", params={"name": "lab"},
                 body={"secrets": {"k": str(tmp_path / "k.pem")}})
    listed = mcp.call("atf_api_benches", op="GET /api/benches/{name}/secrets", params={"name": "lab"})
    literal = mcp.call("atf_api_benches", op="PUT /api/benches/{name}/secrets", params={"name": "lab"},
                       body={"secrets": {"k2": "a literal value"}})

    assert r["isError"] is False and "MIGHAgEA" not in _text(r)
    assert store.secrets("lab", reveal=True)["k"] == pem
    assert "MIGHAgEA" not in _text(listed)
    assert literal["isError"] is True and "file" in _text(literal)
    assert "k2" not in store.secrets("lab")


def test_a_destructive_operation_needs_confirm(mcp, store):
    refused = mcp.call("atf_api_benches", op="DELETE /api/benches/{name}", params={"name": "lab"})
    assert refused["isError"] is True and "confirm" in _text(refused)
    assert store.get_bench("lab") is not None

    done = mcp.call("atf_api_benches", op="DELETE /api/benches/{name}", params={"name": "lab"}, confirm=True)
    assert done["isError"] is False, _text(done)
    assert store.get_bench("lab") is None


def test_no_response_carries_a_token(server, config_dir, store, monkeypatch):
    """An ordinary user's agent enrollment token is read through the MCP, and comes back masked."""
    import os

    from atf.cli import main
    store.upsert_user("operator-1", False, pw="op-pass")
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "op-pass")
    assert main(["remote", "login", "--server", server, "--user", "operator-1"]) == 0
    m = Mcp({**os.environ, "ATF_CONFIG_DIR": str(config_dir)})
    m.request("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})
    try:
        r = m.call("atf_api_agents", op="GET /api/agents/token")
    finally:
        m.close()

    assert r["isError"] is False, _text(r)
    assert json.loads(_text(r)) == {"token": "***"}


def test_an_expired_session_says_to_log_in_again(mcp, config_dir):
    s = json.loads((config_dir / "session.json").read_text())
    (config_dir / "session.json").write_text(json.dumps({**s, "token": "stale"}))

    r = mcp.call("atf_api_benches", op="GET /api/benches")

    assert r["isError"] is True and "atf remote login" in _text(r)


def test_the_guided_flows_are_offered_alongside(mcp):
    names = {t["name"] for t in mcp.request("tools/list")["tools"]}

    assert {"atf_catalog", "atf_suites", "atf_run", "atf_report", "atf_evidence", "atf_requirements",
            "atf_map", "atf_testplan", "atf_benches", "atf_scaffold"} <= names
    assert "atf_api" not in names          # the untyped catch-all is covered by the area tools


def test_a_guided_flow_calls_through_the_remote_session(mcp, config_dir):
    ok = mcp.call("atf_benches", name="lab")
    s = json.loads((config_dir / "session.json").read_text())
    (config_dir / "session.json").write_text(json.dumps({**s, "token": "stale"}))
    expired = mcp.call("atf_benches")

    assert ok["isError"] is False and json.loads(_text(ok))["boards"][0]["name"] == "board-1"
    assert expired["isError"] is True and "atf remote login" in _text(expired)
