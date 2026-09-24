"""A test that never reaches a board is reported, not dropped: a check file another source shadows
is named by discovery, and a test the run names but cannot run leaves an ERROR record saying why —
instead of no record, and a mapped requirement that reads as a gap with nothing behind it."""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"

_CHECK = '''
from atf.core.model import Result, Verdict
from atf.core.registry import register


@register(id="{cid}", title="{cid}")
def check(ctx):
    return Result(Verdict.PASS)
'''


def _source(root: pathlib.Path, files: dict) -> pathlib.Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def _discover_from(*roots):
    os.environ["ATF_CHECK_SOURCES"] = os.pathsep.join(str(r) for r in roots)
    from atf.core import checks
    checks.reload_upstream()
    return checks


def _restore_examples():
    os.environ["ATF_CHECK_SOURCES"] = str(EXAMPLES)
    from atf.core import checks
    checks.reload_upstream()


def test_a_package_two_sources_ship_names_the_files_it_shadows(tmp_path, discovered, example_bench, capsys):
    a = _source(tmp_path / "src-a", {"atf_checks/common/__init__.py": "",
                                     "atf_checks/common/check_a.py": _CHECK.format(cid="_t_shadow_a")})
    b = _source(tmp_path / "src-b", {"atf_checks/common/__init__.py": "",
                                     "atf_checks/common/check_b.py": _CHECK.format(cid="_t_shadow_b")})
    try:
        checks = _discover_from(a, b)             # b goes on sys.path last, so it is searched first
        from atf.core.registry import REGISTRY
        assert "_t_shadow_b" in REGISTRY and "_t_shadow_a" not in REGISTRY

        lost = {u["file"]: u for u in checks.UNLOADED if u["source"] == "src-a"}
        assert set(lost) == {"atf_checks/common/__init__.py", "atf_checks/common/check_a.py"}
        assert "src-b:atf_checks/common" in lost["atf_checks/common/check_a.py"]["reason"]
        assert "src-a:atf_checks/common/check_a.py not loaded" in capsys.readouterr().err

        # a run that names the shadowed test says where it is and why it did not load
        from atf.core import report, runner
        out = tmp_path / "o"
        [r] = runner.run(example_bench, [], out, mgmt_backend="local", expected=["_t_shadow_a"])
        assert r.verdict == "error" and "src-a:atf_checks/common/check_a.py" in r.detail
        meta = json.loads((out / "run-meta.json").read_text())
        assert {u["file"] for u in meta["unloaded"]} >= set(lost)
        report.write([r], out)
        assert "Check files not loaded" in (out / "report.md").read_text()
        assert "Check files not loaded" in (out / "report.html").read_text()
    finally:
        _restore_examples()


def test_a_mapped_test_that_is_not_loaded_is_an_error_record(tmp_path, discovered, example_bench):
    from atf.core import report, runner
    from atf.core.registry import requirement_verdicts, resolve_selection
    sel = {"requirements": [{"id": "acme:X.1", "tests": [{"id": "_t_nowhere"}], "fallback": "TEST_FAIL"},
                            {"id": "acme:X.2", "tests": [], "fallback": "TEST_FAIL"}]}
    [r] = runner.run(example_bench, resolve_selection(sel), tmp_path, mgmt_backend="local", suite_select=sel)
    assert (r.check, r.verdict, r.board) == ("_t_nowhere", "error", "board-1")
    assert "not registered in any loaded check source" in r.detail
    assert (tmp_path / r.evidence).read_text() == r.detail
    # the requirement verdict stays binary — it is still not a pass — but it now has a row saying why
    cells = {q["id"]: q["cells"] for q in requirement_verdicts(sel, [dataclasses.asdict(r)])["requirements"]}
    assert cells == {"acme:X.1": {"board-1": "gap"}, "acme:X.2": {"board-1": "gap"}}
    report.write([r], tmp_path, select=sel)
    md = (tmp_path / "report.md").read_text()
    assert "_t_nowhere" in md and "not registered in any loaded check source" in md
    assert "No test is mapped to this requirement" in md
    assert "No test is mapped" in (tmp_path / "findings" / "acme-X.2-board-1.md").read_text()


def test_a_named_test_for_another_model_says_so(tmp_path, example_bench):
    from atf.core import runner
    from atf.core.model import CheckSpec, Result, Verdict
    spec = CheckSpec(id="_t_other_model", drivers=frozenset(), model="zz-other",
                     fn=lambda ctx: Result(Verdict.PASS))
    [r] = runner.run(example_bench, [spec], tmp_path, mgmt_backend="local", expected=["_t_other_model"])
    assert r.verdict == "error" and "'zz-other' check" in r.detail and "router-x" in r.detail
    assert runner.run(example_bench, [spec], tmp_path, mgmt_backend="local") == []   # not named → nothing


def test_cli_list_and_run_still_resolve_the_registry(discovered, capsys):
    from atf.cli import main
    assert main(["list"]) == 0 and "host-recon" in capsys.readouterr().out
    assert main(["run", "--id", "_t_nowhere"]) == 1
    assert "not registered in any loaded check source: _t_nowhere" in capsys.readouterr().out


def test_named_ids_follow_the_selection():
    from atf.core.registry import named_ids
    sel = {"requirements": [{"id": "r1", "tests": [{"id": "a"}, {"id": "b"}]},
                            {"id": "r2", "tests": [{"id": "a"}, {"id": "c"}]}]}
    assert named_ids(sel) == ["a", "b", "c"]                   # suite order, first occurrence wins
    assert named_ids({"ids": ["x", "y"], "exclude": ["y"]}) == ["x"]
    assert named_ids({"req": ["acme:E.3"]}) == [] and named_ids(None) == []
