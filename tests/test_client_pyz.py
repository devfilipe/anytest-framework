"""`GET /atf.pyz`: the client (`atf remote`, `atf mcp`) as one file the server hands out, like
agent.py — downloaded and run with a bare Python that has no atf installed (isolated mode, so not
even this checkout's site-packages are visible)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request

import pytest

BARE = shutil.which("python3", path="/usr/bin:/bin") or sys.executable


@pytest.fixture
def pyz(server, tmp_path):
    path = tmp_path / "atf.pyz"
    path.write_bytes(urllib.request.urlopen(server + "/atf.pyz").read())
    return path


def _run(pyz, args, env, stdin=""):
    return subprocess.run([BARE, "-I", str(pyz), *args], input=stdin, capture_output=True, text=True,
                          env=env, timeout=60)


@pytest.fixture
def logged_in(server, config_dir, monkeypatch):
    from atf.cli import main
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "admin")
    assert main(["remote", "login", "--server", server, "--user", "admin"]) == 0
    return {**os.environ, "ATF_CONFIG_DIR": str(config_dir)}


def test_the_downloaded_client_runs_remote_commands_without_atf_installed(pyz, logged_in):
    r = _run(pyz, ["remote", "bench", "list"], logged_in)

    assert r.returncode == 0, r.stderr
    assert "lab" in r.stdout.split()


def test_the_downloaded_client_serves_the_mcp(pyz, logged_in):
    lines = "\n".join(json.dumps({"jsonrpc": "2.0", "id": i, "method": m, "params": {}})
                      for i, m in ((1, "initialize"), (2, "tools/list"))) + "\n"

    r = _run(pyz, ["mcp"], logged_in, stdin=lines)

    assert r.returncode == 0, r.stderr
    replies = [json.loads(x) for x in r.stdout.splitlines()]
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert "atf_api_benches" in names and "atf_run" in names


def test_bench_put_takes_a_json_file(pyz, logged_in, tmp_path, store):
    bench = {"agents": {}, "boards": [{"name": "board-9", "model": "router-x", "serial": "9", "creds": {},
                                       "drivers": {"mgmt": {"driver_name": "ip", "ip": "192.0.2.9"}}}]}
    f = tmp_path / "b.json"
    f.write_text(json.dumps(bench))

    r = _run(pyz, ["remote", "bench", "put", "lab9", str(f)], logged_in)

    assert r.returncode == 0, r.stderr
    assert store.get_bench("lab9")["boards"][0]["drivers"]["mgmt"]["ip"] == "192.0.2.9"
