"""AI resource pack lifecycle: the pack is deployed to the developer's host, the connect listing
reflects what was SENT (not stray files), no bytecode ships, and disconnect removes exactly the
framework-deployed files — never the user's own."""
from __future__ import annotations

import base64
import io
import pathlib
import tarfile

from atf import agent

PACK_DIR = pathlib.Path(__file__).resolve().parent.parent / "atf" / "agent_pack"


def _pack_b64(tmp_pyc: pathlib.Path | None = None) -> str:
    """Build the pack tarball the way the server does — excluding __pycache__/*.pyc."""
    def _pack_only(ti):
        parts = ti.name.split("/")
        return None if ("__pycache__" in parts or parts[-1].endswith(".pyc")) else ti
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(PACK_DIR, arcname=".", filter=_pack_only)
    return base64.b64encode(buf.getvalue()).decode()


def test_pack_deploy_lists_sent_files_no_bytecode(tmp_path):
    res = agent._ai_enable(str(tmp_path / "ai"), _pack_b64(), "http://s", "tok", aid="a1", sources=["/x"])
    assert res["ok"]
    files = res["files"]
    assert ".mcp.json" in files and ".claude/settings.json" in files and "CLAUDE.md" in files
    assert ".atf-ai.env" in files                       # framework-created config is tracked too
    assert not any(f.endswith(".pyc") or "__pycache__" in f for f in files)   # never ship bytecode
    assert (tmp_path / "ai" / ".atf-ai-manifest.json").is_file()


def test_disconnect_removes_only_framework_files(tmp_path):
    dest = tmp_path / "ai"
    agent._ai_enable(str(dest), _pack_b64(), "http://s", "tok", aid="a1", sources=["/x"])
    (dest / "my-notes.md").write_text("mine")           # a file the user placed alongside the pack
    (dest / ".claude" / "skills" / "atf-run" / "user.txt").write_text("mine2")

    r = agent._ai_disable(str(dest))
    assert r["ok"]
    left = sorted(str(p.relative_to(dest)) for p in dest.rglob("*") if p.is_file())
    assert left == [".claude/skills/atf-run/user.txt", "my-notes.md"]   # only the user's files survive
    assert not (dest / "CLAUDE.md").exists() and not (dest / ".mcp.json").exists()
    assert not (dest / ".atf-ai-manifest.json").exists()


def test_disconnect_prunes_dir_when_nothing_user_left(tmp_path):
    dest = tmp_path / "ai"
    agent._ai_enable(str(dest), _pack_b64(), "http://s", "tok", aid="a1", sources=["/x"])
    agent._ai_disable(str(dest))
    assert not dest.exists()                             # pack-only dir is removed entirely


def _load_mcp():
    import importlib.util
    spec = importlib.util.spec_from_file_location("atf_mcp", PACK_DIR / "atf_mcp.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_mcp_tools_present_and_wired(monkeypatch):
    m = _load_mcp()
    names = {t[0]["name"] for t in m.TOOLS}
    assert {"atf_evidence", "atf_benches", "atf_requirements", "atf_map"} <= names   # phase-1 tools
    calls = []

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        return '{"ok": true}'
    monkeypatch.setattr(m, "_api", fake_api)

    m._BY_NAME["atf_evidence"][1]({"run_id": "r1", "path": "evidence/x-b1.txt"})
    assert calls[-1][0] == "GET" and calls[-1][1].startswith("/api/evidence?") and "run_id=r1" in calls[-1][1]

    m._BY_NAME["atf_benches"][1]({})
    assert calls[-1] == ("GET", "/api/benches", None)
    m._BY_NAME["atf_benches"][1]({"name": "lab"})
    assert calls[-1][1] == "/api/benches/lab"

    m._BY_NAME["atf_requirements"][1]({})
    assert calls[-1][1] == "/api/requirements/frameworks"
    m._BY_NAME["atf_requirements"][1]({"framework": "vivo"})
    assert "framework=vivo" in calls[-1][1]

    m._BY_NAME["atf_map"][1]({"name": "s1", "model": "tmd400g",
                              "requirements": [{"id": "vivo:C.4", "tests": ["ping-dcn"]}]})
    put = [c for c in calls if c[0] == "PUT"][-1]
    assert put[1] == "/api/suites/s1"
    sel = put[2]["select"]
    assert sel["model"] == "tmd400g"
    assert sel["requirements"][0] == {"id": "vivo:C.4", "fallback": "TEST_FAIL", "tests": [{"id": "ping-dcn"}]}
    assert any(c[0] == "POST" and c[1] == "/api/suites/validate" for c in calls)

    assert "atf_testplan" in names
    m._BY_NAME["atf_testplan"][1]({"op": "list"})
    assert calls[-1] == ("GET", "/api/test-plans", None)
    m._BY_NAME["atf_testplan"][1]({"op": "save", "name": "tp1", "suite": "s", "bench": "b", "board": "b1"})
    save = [c for c in calls if c[0] == "PUT" and c[1] == "/api/test-plans/tp1"][-1]
    assert save[2]["suite"] == "s" and save[2]["board"] == "b1"
