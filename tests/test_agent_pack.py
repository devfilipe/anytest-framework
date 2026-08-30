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
