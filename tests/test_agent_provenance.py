"""Provenance of a run through an agent's working tree: the run folder's `run-meta.json` records, for
each of the agent's sources, the git state AT TREE UPLOAD (the code that actually ran), not the
state the agent saw when it registered. A long-lived agent must not label a run with an old commit
or a stale "dirty" flag. The targets are local (127.0.0.1); no board is involved."""
from __future__ import annotations

import json
import subprocess
import threading
import time

import httpx
import pytest

CHECK = '''
from atf.core.model import Result, Verdict
from atf.core.registry import register


@register(id="agent-provenance-probe", title="passes")
def _probe(ctx):
    return Result(Verdict.PASS)
'''


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def dev_repo(tmp_path):
    """A developer's check-source working tree: a git repo with one committed check."""
    repo = tmp_path / "dev-checks"
    model_dir = repo / "atf_checks" / "router_x"
    model_dir.mkdir(parents=True)
    (model_dir / "__init__.py").write_text("")
    (model_dir / "probe.py").write_text(CHECK)
    (repo / ".gitignore").write_text("scratch/\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "-c", "user.name=dev", "-c", "user.email=dev@example.com", "add", "-A")
    _git(repo, "-c", "user.name=dev", "-c", "user.email=dev@example.com", "commit", "-qm", "first")
    return repo


def _commit(repo, msg):
    _git(repo, "-c", "user.name=dev", "-c", "user.email=dev@example.com", "commit", "-qam", msg)
    return _git(repo, "rev-parse", "--short", "HEAD")


@pytest.fixture
def user(server, store, tmp_path, monkeypatch):
    """A logged-in regular user (the admin account can't run an agent) and their enrollment token."""
    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "store.db"))   # the worker's bench lookup
    monkeypatch.setenv("ATF_WORK", str(tmp_path / "work"))
    admin = httpx.Client(base_url=server)
    admin.headers["Authorization"] = "Bearer " + admin.post(
        "/api/admin/login", json={"user": "admin", "password": "admin"}).json()["token"]
    admin.post("/api/users", json={"username": "dev", "password": "pw", "is_admin": False})
    c = httpx.Client(base_url=server, timeout=30)
    c.headers["Authorization"] = "Bearer " + c.post(
        "/api/admin/login", json={"user": "dev", "password": "pw"}).json()["token"]
    return c


@pytest.fixture
def agent(server, user, dev_repo):
    """The real agent loop, connected to the server with the dev repo as its only source."""
    from atf import agent as agent_mod
    tok = user.get("/api/agents/token").json()["token"]
    t = threading.Thread(target=agent_mod.run, args=(server, tok, [str(dev_repo)], "dev-lap"), daemon=True)
    t.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        ags = user.get("/api/agents").json()
        ags = ags.get("agents", ags) if isinstance(ags, dict) else ags
        if ags:
            break
        time.sleep(0.1)
    else:
        pytest.fail("agent did not register")
    yield ags[0]["id"]
    user.delete(f"/api/agents/{ags[0]['id']}")
    t.join(timeout=30)


def _run_and_read_meta(user, tmp_path) -> dict:
    r = user.post("/api/run", json={"bench": "lab", "ids": ["agent-provenance-probe"],
                                     "board": ["board-1"], "mgmt_backend": "local"}).json()
    assert r["started"], r
    deadline = time.time() + 120
    while time.time() < deadline:
        st = user.get("/api/run/status").json()
        if not st["active"] and st.get("last"):
            break
        time.sleep(0.2)
    last = st["last"]
    assert last["type"] == "done", last
    return json.loads((tmp_path / "out" / "runs" / last["run_id"] / "run-meta.json").read_text())


def _dev_source(meta):
    return next(s for s in meta["sources"] if s["name"] == "dev-checks")


def test_a_run_records_the_commit_and_clean_state_at_upload(server, agent, user, dev_repo, tmp_path):
    # the agent registered on the first commit; the developer then edits, commits and runs
    (dev_repo / "atf_checks" / "router_x" / "probe.py").write_text(CHECK + "\n# edited\n")
    head = _commit(dev_repo, "edit the probe")
    (dev_repo / "scratch").mkdir()
    (dev_repo / "scratch" / "notes.txt").write_text("ignored by .gitignore")

    src = _dev_source(_run_and_read_meta(user, tmp_path))

    assert (src["commit"], src["ref"], src["dirty"], src["state_at"]) == (head, "main", False, "upload")
    assert src["agent"] == "dev-lap" and src["owner"] == "dev"


def test_a_run_records_an_uncommitted_change_as_dirty(server, agent, user, dev_repo, tmp_path):
    head = _git(dev_repo, "rev-parse", "--short", "HEAD")
    (dev_repo / "atf_checks" / "router_x" / "probe.py").write_text(CHECK + "\n# not committed\n")

    src = _dev_source(_run_and_read_meta(user, tmp_path))

    assert (src["commit"], src["dirty"], src["state_at"]) == (head, True, "upload")


def test_an_older_agent_without_upload_state_falls_back_to_the_registration_state(tmp_path, monkeypatch, store):
    """An agent built before the upload carried git state: its tree is labelled with what it reported
    at registration, marked as such."""
    from atf import agent as agent_mod
    from atf.web.agents import AgentHub
    from atf.web.server import _agent_worker_run

    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "store.db"))
    monkeypatch.setenv("ATF_WORK", str(tmp_path / "work"))
    store.upsert_bench("loop", {"agents": {}, "boards": [
        {"name": "board-1", "model": "", "serial": "1", "creds": {},
         "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1"}}}]})
    src = tmp_path / "dev-checks" / "atf_checks" / "router_x"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "probe.py").write_text(CHECK)
    hub = AgentHub()
    s = hub.register("old-lap", [{"name": "dev-checks", "branch": "main", "head": "abc1234", "dirty": True}],
                     {}, "linux", owner="dev")
    old_tar = agent_mod._tar([tmp_path / "dev-checks"], manifest=False)   # what an older agent sends

    def _upload():
        cmd = s.cmds.get(timeout=10)
        hub.receive_tree(s, cmd["token"], old_tar)
    threading.Thread(target=_upload, daemon=True).start()

    _recs, _dev, _out, meta = _agent_worker_run(hub, s, "loop", "board-1",
                                                {"ids": ["agent-provenance-probe"]}, "local")

    got = _dev_source(meta)
    assert (got["commit"], got["ref"], got["dirty"], got["state_at"]) == ("abc1234", "main", True, "register")


def test_running_an_agent_working_tree_from_the_agents_page_returns_its_records(server, agent, user):
    r = user.post(f"/api/agents/{agent}/run", json={"bench": "lab", "board": "board-1",
                                                   "ids": ["agent-provenance-probe"], "mgmt_backend": "local"})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and [x["check"] for x in body["records"]] == ["agent-provenance-probe"]
