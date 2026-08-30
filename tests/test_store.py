"""Config store: built-in entities, CRUD, bench round-trip, legacy migration, backup/restore."""
from __future__ import annotations

import json

import pytest


def test_builtin_driver_types_seeded(store):
    by = {d["name"]: d for d in store.list_inv_drivers()}
    assert {"serial", "ip"} <= set(by)
    assert [p["name"] for p in by["ip"]["props"]] == ["agent", "ip"]
    assert [p["name"] for p in by["serial"]["props"]] == ["agent", "transport", "device", "baud"]
    assert all(p.get("description") for p in by["ip"]["props"])       # descriptions present


def test_builtin_power_cycle_action(store):
    pc = next(a for a in store.list_inv_actions() if a["name"] == "power-cycle")
    assert [s["name"] for s in pc["signals"]] == ["off", "on", "status"]
    assert pc["description"]


def test_driver_entity_crud(store):
    store.upsert_inv_driver("jtag", "JTAG probe",
                            [{"name": "agent", "description": "node with the probe"},
                             {"name": "port", "description": "OpenOCD TCP port"}])
    d = next(x for x in store.list_inv_drivers() if x["name"] == "jtag")
    assert d["description"] == "JTAG probe"
    assert [p["name"] for p in d["props"]] == ["agent", "port"]
    store.delete_inv_driver("jtag")
    assert "jtag" not in {x["name"] for x in store.list_inv_drivers()}


def test_action_entity_crud(store):
    store.upsert_inv_action("reset-btn", "physical reset",
                            [{"name": "press", "description": "press & release"}])
    a = next(x for x in store.list_inv_actions() if x["name"] == "reset-btn")
    assert [s["name"] for s in a["signals"]] == ["press"]
    assert a["signals"][0]["description"] == "press & release"


def test_bench_roundtrip_driver_type_alias_and_creds(store):
    data = {"agents": {"rpi": {"platform": "linux", "host": "192.0.2.1",
                               "ssh": {"user": "pi", "password_ref": "pi"}}},
            "boards": [{"name": "b1", "model": "router-x", "serial": "1",
                        "creds": {"root": {"user": "root", "password_ref": "root"}},
                        "drivers": {"mgmt": {"driver_name": "ip", "agent": "", "ip": "127.0.0.1"},
                                    "console": {"driver_name": "serial", "agent": "rpi",
                                                "device": "/dev/ttyUSB0", "baud": 115200}}}]}
    store.upsert_bench("b", data)
    bd = store.get_bench("b")["boards"][0]
    assert "mgmt" not in bd                              # board carries no static mgmt block
    assert bd["creds"] == {"root": {"user": "root", "password_ref": "root"}}  # creds are bench-scoped
    assert bd["drivers"]["mgmt"]["type"] == "ip" and bd["drivers"]["mgmt"]["ip"] == "127.0.0.1"
    assert bd["drivers"]["console"]["type"] == "serial" and bd["drivers"]["console"]["device"] == "/dev/ttyUSB0"


def test_inventory_board_is_slim(store):
    # a board upserted through a bench keeps only name/model/serial in the inventory
    store.upsert_bench("b", {"boards": [{"name": "b1", "model": "m", "serial": "1", "creds": {}, "drivers": {}}]})
    inv = next(x for x in store.list_inv_boards() if x["name"] == "b1")
    assert set(inv) >= {"name", "model", "serial"}
    assert "creds" not in inv                            # creds moved off the inventory board


def test_legacy_driver_migration(store):
    """A pre-entity bench (bench_vector with literal aliases + no driver_name, mgmt_ip on the board)
    folds into serial/ip driver types with the ip carried into the driver config."""
    con = store.con
    con.execute("INSERT INTO bench(name) VALUES('lb')")
    bid = con.execute("SELECT id FROM bench WHERE name='lb'").fetchone()[0]
    con.execute("INSERT INTO inv_board(name,model,serial,mgmt_ip) VALUES('bd','m','1','10.0.0.9')")
    con.execute("INSERT INTO bench_board(bench_id,board_name) VALUES(?,?)", (bid, "bd"))
    con.execute("INSERT INTO bench_vector(bench_id,board_name,vector,driver_name,config_json) VALUES(?,?,?,?,?)",
                (bid, "bd", "mgmt", "", "{}"))
    con.execute("INSERT INTO bench_vector(bench_id,board_name,vector,driver_name,config_json) VALUES(?,?,?,?,?)",
                (bid, "bd", "console", "", json.dumps({"agent": "rpi"})))
    con.commit()
    assert store.migrate_drivers_to_inventory() == 2
    bd = store.get_bench("lb")["boards"][0]
    assert bd["drivers"]["mgmt"]["type"] == "ip"
    assert bd["drivers"]["mgmt"]["ip"] == "10.0.0.9"     # folded from the old inv_board.mgmt_ip
    assert bd["drivers"]["console"]["type"] == "serial"


def test_backup_restore_roundtrip(store, tmp_path):
    store.upsert_inv_driver("jtag", "j", [{"name": "port", "description": "p"}])
    store.set_secrets("__inventory__", {"s1": "secret-value"})
    snap = tmp_path / "snap.db"
    store.backup(snap)
    from atf.store import open_repo
    dst = open_repo(db_path=str(tmp_path / "dst.db"), app_secret="test-secret")
    dst.restore(snap)
    assert "jtag" in {d["name"] for d in dst.list_inv_drivers()}
    assert dst.secrets("__inventory__", reveal=True)["s1"] == "secret-value"   # secrets survive (same APP_SECRET)


def test_report_persists_run_meta(store):
    """A report keeps its run-meta snapshot (bench under test + check-source versions as loaded), so
    the web report view can show element identity + versions durably for historical runs."""
    meta = {"bench": {"name": "b", "boards": [{"name": "b1", "model": "router-x", "serial": "1"}]},
            "sources": [{"name": "checks-common", "commit": "abc1234", "ref": "main", "dirty": True}]}
    store.add_report(run_id="R1", owner="u", suite="s", bench="b", board="b1",
                     counts={"pass": 1}, select={}, meta=meta)
    got = store.get_report("R1", "u", is_admin=True)
    assert got["meta"]["bench"]["boards"][0]["model"] == "router-x"
    assert got["meta"]["sources"][0]["commit"] == "abc1234" and got["meta"]["sources"][0]["dirty"]


def _src(store, name):
    return next(x for x in store.list_check_sources() if x["name"] == name)


def test_path_source_syncs_and_unsync_preserves_dir(store, tmp_path):
    """A `path` check-source loads a server-local directory (no clone) and records who synced it;
    unsync unlinks it but NEVER deletes the user's own directory."""
    from atf.core.checks import sync_sources
    d = tmp_path / "mychecks"
    (d / "atf_checks" / "common" / "host").mkdir(parents=True)
    store.upsert_check_source("localsrc", str(d), kind="path")
    sync_sources(store, by="alice")
    s = _src(store, "localsrc")
    assert s["kind"] == "path" and s["last_status"] == "ok" and s["last_sync_by"] == "alice"
    assert s["checkout"] == str(d.resolve())
    info = store.clear_check_source_sync("localsrc")
    assert info["kind"] == "path" and d.is_dir()          # unlinked, but the directory survives


def test_path_source_without_atf_checks_errors(store, tmp_path):
    from atf.core.checks import sync_sources
    d = tmp_path / "empty"
    d.mkdir()
    store.upsert_check_source("bad", str(d), kind="path")
    sync_sources(store)
    assert _src(store, "bad")["last_status"] == "error"


def test_git_source_fetches_a_full_ref(store, tmp_path):
    """A full ref (e.g. a Gerrit change/patchset `refs/changes/68/85368/1`) is fetched + checked
    out — not treated as a branch (which the first clone's --branch can't do)."""
    import pathlib
    import subprocess as sp
    origin = tmp_path / "origin"
    origin.mkdir()

    def g(*a):
        sp.run(["git", *a], cwd=origin, check=True, capture_output=True)

    g("init", "-q", "-b", "master")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (origin / "atf_checks").mkdir()
    (origin / "atf_checks" / "base.py").write_text("# master only\n")
    g("add", "-A")
    g("commit", "-qm", "master")
    # a change that is NOT on master: extra file, parked on a gerrit-style ref
    g("checkout", "-q", "-b", "tmp")
    (origin / "atf_checks" / "changed.py").write_text("# the change\n")
    g("add", "-A")
    g("commit", "-qm", "change")
    sha = sp.run(["git", "rev-parse", "--short", "HEAD"], cwd=origin, capture_output=True, text=True).stdout.strip()
    g("update-ref", "refs/changes/68/85368/1", "HEAD")
    g("checkout", "-q", "master")
    g("branch", "-qD", "tmp")

    from atf.core import checks
    store.upsert_check_source("chg", f"file://{origin}", ref="refs/changes/68/85368/1", kind="git")
    checks.sync_sources(store, by="dev")
    s = _src(store, "chg")
    assert s["last_status"] == "ok" and s["last_commit"] == sha
    co = pathlib.Path(s["checkout"])
    assert (co / "atf_checks" / "changed.py").is_file()      # the patchset content, not master


def test_removing_last_source_drops_orphan_manual_tests(monkeypatch, tmp_path):
    """Unsync/delete of the last source must drop its Markdown manual tests too — even when no
    `atf_checks` namespace remains to import (regression: they used to linger as source-less tests)."""
    import pathlib
    from atf.core import checks
    from atf.core.registry import REGISTRY
    examples = pathlib.Path(__file__).resolve().parent.parent / "examples"
    src = tmp_path / "src"
    (src / "atf_checks" / "m").mkdir(parents=True)
    (src / "atf_checks" / "m" / "t.md").write_text("---\nid: orphan-man\n---\nsteps\n")
    try:
        monkeypatch.setenv("ATF_CHECK_SOURCES", str(src))
        checks.reload_upstream()
        assert "orphan-man" in REGISTRY
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setenv("ATF_CHECK_SOURCES", str(empty))    # source removed — no atf_checks package
        checks.reload_upstream()
        assert "orphan-man" not in REGISTRY                    # the orphan manual is gone
    finally:
        monkeypatch.setenv("ATF_CHECK_SOURCES", str(examples))  # restore the example checks for other tests
        checks.reload_upstream()
        REGISTRY.pop("orphan-man", None)


def test_check_source_records_commit_and_by(store):
    """Provenance: the loaded sha1 + the user who triggered the sync are persisted (no credentials)."""
    store.upsert_check_source("r", "ssh://u@gerrit:29418/proj")
    store.set_check_source_status("r", "ok", "synced", "/co", "2026-01-01", commit="abc1234", by="bob")
    s = _src(store, "r")
    assert s["last_commit"] == "abc1234" and s["last_sync_by"] == "bob" and not s["has_token"]


def test_restore_rejects_non_db(store, tmp_path):
    bad = tmp_path / "bad.db"
    bad.write_text("not a database")
    with pytest.raises(ValueError):
        store.restore(bad)
