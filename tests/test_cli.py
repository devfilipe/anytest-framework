"""CLI parity with the config store: `atf run --bench/--suite <name>` and `atf suites` resolve
store entities (not only YAML files), mirroring the web + agent-worker. A local file still wins.
Also covers the requirement-centric report (bench under test + check/requirement versions)."""
from __future__ import annotations

import json
import pathlib


def _point_cli_at_store(tmp_path, monkeypatch):
    """A fresh store the CLI's open_repo() will open, seeded with one bench + one suite."""
    from atf.store import open_repo
    db = tmp_path / "cli-store.db"
    monkeypatch.setenv("DATABASE_URL", f"file:{db}")
    monkeypatch.setenv("APP_SECRET", "cli-secret")
    repo = open_repo(db_path=str(db), app_secret="cli-secret")
    repo.ensure_admin()
    repo.upsert_bench("Store Bench", {"agents": {}, "boards": [
        {"name": "b1", "model": "router-x", "serial": "1", "creds": {},
         "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1"}}}]})
    repo.upsert_suite("store-suite", {"title": "From Store", "select": {"ids": ["host-recon"]}})
    return repo


def test_suites_lists_store_and_file(tmp_path, monkeypatch, discovered, capsys):
    _point_cli_at_store(tmp_path, monkeypatch)
    from atf.cli import main
    assert main(["suites"]) == 0
    out = capsys.readouterr().out
    assert "store-suite" in out and "[store]" in out


def test_run_resolves_store_bench_and_suite(tmp_path, monkeypatch, discovered, capsys):
    _point_cli_at_store(tmp_path, monkeypatch)
    from atf.cli import main
    # store bench name + store suite name; --board __none__ selects no board so nothing is contacted,
    # but resolving both proves the CLI reads the store (a file path would FileNotFound before this).
    rc = main(["run", "--bench", "Store Bench", "--suite", "store-suite",
               "--board", "__none__", "--mgmt-backend", "local", "--out", str(tmp_path / "o")])
    assert rc == 0
    assert (tmp_path / "o" / "results.json").is_file()


def test_run_unknown_bench_errors(tmp_path, monkeypatch, discovered, capsys):
    _point_cli_at_store(tmp_path, monkeypatch)
    from atf.cli import main
    rc = main(["run", "--bench", "no-such-bench", "--id", "host-recon",
               "--mgmt-backend", "local", "--out", str(tmp_path / "o")])
    assert rc == 2
    assert "bench not found" in capsys.readouterr().out


def test_report_is_requirement_centric_with_versions(tmp_path, monkeypatch, discovered):
    """A mapped-suite run writes run-meta.json (bench under test + source versions + the map) and a
    report that presents the bench, the check-source versions as loaded, and each requirement's
    per-board status with the mapped tests' provenance."""
    repo = _point_cli_at_store(tmp_path, monkeypatch)
    repo.upsert_requirement("rpt", "R1", {"title": "Sample requirement", "desc": "d",
                                          "verify": "external probe", "priority": "1"})
    repo.upsert_suite("mapped", {"title": "Mapped", "select": {"requirements": [
        {"id": "rpt:R1", "tests": [{"id": "host-recon"}], "fallback": "TEST_FAIL"}]}})
    from atf.core import requirements as reqmeta
    reqmeta.invalidate()
    from atf.cli import main
    out = tmp_path / "o"
    assert main(["run", "--bench", "Store Bench", "--suite", "mapped",
                 "--mgmt-backend", "local", "--out", str(out)]) == 0

    meta = json.loads((out / "run-meta.json").read_text())
    assert meta["bench"]["name"] == "Store Bench"
    assert meta["bench"]["boards"][0]["model"] == "router-x"
    assert meta["select"]["requirements"][0]["id"] == "rpt:R1"          # suite map persisted
    assert meta["sources"] and meta["sources"][0]["commit"]            # a version was captured

    report = (out / "report.md").read_text()
    assert "Element(s) under test" in report and "router-x" in report  # bench presented
    assert "Check sources — versions as loaded" in report
    assert "rpt:R1" in report and "Sample requirement" in report        # requirement text
    assert "Overall:" in report and "host-recon" in report              # requirement verdict + mapped test
    assert "version `" in report                                         # requirement content version
    matrix = (out / "matrix.md").read_text()
    assert "rpt:R1" in matrix and "v:" in matrix                         # requirement + version in the matrix
    assert (out / "findings" / "rpt-R1-b1.md").is_file()
    # standalone HTML report — requirement-first, self-contained
    html = (out / "report.html").read_text()
    assert html.startswith("<!doctype html>") and "Element(s) under test" in html
    assert "rpt:R1" in html and "Sample requirement" in html and "<style>" in html
    assert html.count("<div") == html.count("</div>")          # well-formed: no unclosed requirement cards


def test_run_local_file_bench_still_works(tmp_path, monkeypatch, discovered):
    """A real YAML path takes precedence over the store lookup."""
    _point_cli_at_store(tmp_path, monkeypatch)
    from atf.cli import main
    lab = pathlib.Path(__file__).resolve().parent.parent / "examples" / "benches" / "lab.yaml"
    rc = main(["run", "--id", "host-recon", "--bench", str(lab),
               "--mgmt-backend", "local", "--out", str(tmp_path / "o2")])
    assert rc == 0
    assert (tmp_path / "o2" / "results.json").is_file()
