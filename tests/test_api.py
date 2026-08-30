"""HTTP API via FastAPI TestClient: auth gate, entity CRUD, bench round-trip, run status/cancel,
test-plan capability validation."""
from __future__ import annotations


def test_auth_gate(client):
    assert client.get("/api/inventory/drivers").status_code == 200      # authed fixture
    # docs are open; a data endpoint without a token is 401
    bare = client.__class__(client.app)
    assert bare.get("/api/docs").status_code == 200
    assert bare.get("/api/inventory/drivers").status_code == 401


def test_version_endpoint_is_open(client):
    bare = client.__class__(client.app)                 # no token
    r = bare.get("/api/version")
    assert r.status_code == 200                          # open (shown in the UI before/without auth)
    assert set(r.json()) >= {"commit", "ref", "dirty"}


def test_manual_check_source_view_edit(client, tmp_path):
    """A server-side manual test (from a git/path check-source) can be viewed AND saved via the
    check source endpoint — previously 409'd ('manual checks are edited as data')."""
    from atf.core.registry import REGISTRY, register_md_manual
    f = tmp_path / "m.md"
    f.write_text("---\nid: t-man\n---\n## steps\n1. do it\n")
    register_md_manual("t-man", model="x", body="steps", path=str(f))
    try:
        r = client.get("/api/checks/t-man/source")
        assert r.status_code == 200 and r.json()["manual"] and "## steps" in r.json()["source"]
        w = client.put("/api/checks/t-man/source", json={"source": "---\nid: t-man\n---\n## edited\n"})
        assert w.json()["ok"] and "## edited" in f.read_text()
    finally:
        REGISTRY.pop("t-man", None)


def test_git_source_check_is_read_only(client, store, tmp_path):
    """A test synced from a git Repository is view-only — its clone is transient (Sync overwrites)."""
    co = tmp_path / "clone"
    (co / "atf_checks").mkdir(parents=True)
    md = co / "atf_checks" / "t.md"
    md.write_text("---\nid: g-man\n---\nsteps\n")
    store.upsert_check_source("gitrepo", "https://h/o/r.git", kind="git")
    store.set_check_source_status("gitrepo", "ok", "synced", str(co), "2026-01-01")
    from atf.core.registry import REGISTRY, register_md_manual
    register_md_manual("g-man", model="x", body="s", path=str(md))
    try:
        assert client.get("/api/checks/g-man/source").json()["editable"] is False
        assert client.put("/api/checks/g-man/source", json={"source": "x"}).status_code == 409
    finally:
        REGISTRY.pop("g-man", None)


def test_repo_provides_counts_manual_md(tmp_path):
    """A check-source's test count includes Markdown manual tests, not just .py (they were missed)."""
    from atf.web.server import _repo_provides
    d = tmp_path / "repo" / "atf_checks" / "m"
    d.mkdir(parents=True)
    (d / "a.py").write_text("# auto\n")
    (d / "__init__.py").write_text("")                  # package marker, not a check
    (d / "t.md").write_text("---\nid: t\n---\nbody\n")   # a manual test
    assert _repo_provides(str(tmp_path / "repo"))["checks"] == 2   # a.py + t.md


def test_manual_test_carries_source_path():
    """A manual test records its .md path so it can be attributed to its source repo in the catalog."""
    from atf.core.registry import REGISTRY, register_md_manual
    register_md_manual("tmp-manual-x", model="x", body="b", path="/repo/atf_checks/x/t.md")
    try:
        assert REGISTRY["tmp-manual-x"].path == "/repo/atf_checks/x/t.md"
        assert REGISTRY["tmp-manual-x"].mode == "manual"
    finally:
        REGISTRY.pop("tmp-manual-x", None)


def test_git_slug_variants():
    from atf.web.server import _git_slug
    assert _git_slug("https://github.com/devfilipe/anytest-checks-common.git") == "devfilipe/anytest-checks-common"
    assert _git_slug("git@github.com:devfilipe/repo.git") == "devfilipe/repo"
    assert _git_slug("ssh://u@gerrit.example.com:29418/esw-misc-pentesting") == "esw-misc-pentesting"


def test_unsync_drops_checkout(client, store, tmp_path):
    co = tmp_path / "clone"
    (co / "atf_checks").mkdir(parents=True)
    store.upsert_check_source("myrepo", "https://github.com/o/r.git")
    store.set_check_source_status("myrepo", "ok", "synced", str(co), "2026-01-01")
    r = client.post("/api/check-sources/myrepo/unsync")
    assert r.status_code == 200 and r.json()["ok"]
    assert not co.exists()                               # local clone removed
    s = next(x for x in store.list_check_sources() if x["name"] == "myrepo")
    assert not s.get("checkout") and not s.get("last_status")   # config kept, sync forgotten


def test_builtin_entities_via_api(client):
    drivers = {d["name"] for d in client.get("/api/inventory/drivers").json()}
    assert {"serial", "ip"} <= drivers
    actions = {a["name"] for a in client.get("/api/actions/catalog").json()}
    assert "power-cycle" in actions


def test_driver_crud_via_api(client):
    r = client.post("/api/inventory/drivers",
                    json={"name": "jtag", "description": "J", "props": [{"name": "port", "description": "p"}]})
    assert r.json()["ok"]
    d = next(x for x in client.get("/api/inventory/drivers").json() if x["name"] == "jtag")
    assert d["description"] == "J" and d["props"][0]["name"] == "port"
    assert client.delete("/api/inventory/drivers/jtag").json()["ok"]


def test_bench_roundtrip_via_api(client):
    body = {"agents": {}, "boards": [{"name": "b1", "model": "router-x", "serial": "1",
                                      "creds": {"root": {"user": "root", "password_ref": "root"}},
                                      "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1"}}}]}
    assert client.put("/api/benches/tb", json=body).json()["ok"]
    bd = client.get("/api/benches/tb").json()["boards"][0]
    assert "mgmt" not in bd
    assert bd["drivers"]["mgmt"]["type"] == "ip" and bd["drivers"]["mgmt"]["ip"] == "127.0.0.1"
    assert bd["creds"]["root"]["user"] == "root"


def test_run_status_and_cancel_when_idle(client):
    assert client.get("/api/run/status").json()["active"] is False
    assert client.post("/api/run/cancel").json()["cancelled"] is False


def test_testplan_capabilities(client):
    client.put("/api/suites/s", json={"select": {"requirements": [
        {"id": "acme:X", "tests": [{"id": "host-recon"}]}]}})
    client.put("/api/benches/tb", json={"boards": [
        {"name": "b1", "model": "router-x", "serial": "1", "creds": {},
         "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1"}}}]})
    d = client.post("/api/test-plans/capabilities",
                    json={"suite": "s", "bench": "tb", "board": "b1"}).json()
    assert "mgmt" in d["need_drivers"]
    assert d["missing_drivers"] == []        # the board provides the mgmt driver the test needs


def test_report_evidence_html_and_package(client, store, tmp_path):
    """Per-run artifacts persist under out/runs/<id>: evidence is served with run_id, the HTML report
    opens inline, and the .zip package bundles report + evidence."""
    import io
    import json
    import zipfile
    out = tmp_path / "out"
    rd = out / "runs" / "run-1" / "evidence"
    rd.mkdir(parents=True)
    (rd / "host-recon-b1.txt").write_text("EVIDENCE-XYZ")
    (out / "runs" / "run-1" / "report.html").write_text("<!doctype html><html>RPT-HTML</html>")
    (out / "runs" / "run-1" / "report.md").write_text("# md report")
    rec = {"run_id": "run-1", "board": "b1", "check": "host-recon", "requirements": [],
           "verdict": "pass", "severity": "info", "title": "t", "detail": "d",
           "evidence": "evidence/host-recon-b1.txt", "source": "examples"}
    with (out / "runs.jsonl").open("a") as f:
        f.write(json.dumps(rec) + "\n")
    store.add_report(run_id="run-1", owner="admin", suite="s", bench="b", board="b1",
                     counts={"pass": 1}, meta={})

    # evidence resolves from the run's own dir (not a shared/overwritten one)
    ev = client.get("/api/evidence", params={"path": "evidence/host-recon-b1.txt", "run_id": "run-1"})
    assert ev.status_code == 200 and ev.text == "EVIDENCE-XYZ"
    # HTML export is inline + serves the persisted artifact
    h = client.get("/api/reports/run-1/export", params={"format": "html"})
    assert h.status_code == 200 and "text/html" in h.headers["content-type"] and "RPT-HTML" in h.text
    # results package bundles the run dir + a stored-row fallback
    z = client.get("/api/reports/run-1/download")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert "report.html" in names and "evidence/host-recon-b1.txt" in names and "report-row.json" in names


def test_testplan_capabilities_flags_missing(client):
    client.put("/api/suites/s2", json={"select": {"requirements": [
        {"id": "acme:X", "tests": [{"id": "host-recon"}]}]}})
    client.put("/api/benches/tb2", json={"boards": [
        {"name": "b2", "model": "router-x", "serial": "1", "creds": {}, "drivers": {}}]})   # no mgmt driver
    d = client.post("/api/test-plans/capabilities",
                    json={"suite": "s2", "bench": "tb2", "board": "b2"}).json()
    assert d["missing_drivers"] == ["mgmt"]
