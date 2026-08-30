"""SDK/runtime: alias-keyed ctx, capability gating, selection, run + cancel."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest


def test_ctx_is_alias_keyed():
    from atf.access.channels.ip import IpChannel
    from atf.access.host import HostProbe
    from atf.core.inventory import Board
    from atf.core.model import Ctx
    ch = IpChannel({"ip": "127.0.0.1"}, None, {})
    ctx = Ctx(board=Board(name="b", model=""), host=HostProbe(), out_root=Path("/tmp"), drivers={"mgmt": ch})
    assert ctx.mgmt is ch                       # ctx.<alias>
    assert ctx.available_drivers == {"mgmt"}
    with pytest.raises(AttributeError):
        _ = ctx.craft                           # an unwired alias is absent


def test_board_has_no_mgmt_attr():
    from atf.core.inventory import Board
    assert not hasattr(Board(name="b", model=""), "mgmt")   # the static Mgmt field is gone


def test_available_drivers_gating_by_type():
    from atf.core import inventory
    from atf.core.runner import available_drivers
    data = {"agents": {"rpi": {"platform": "linux", "host": "x", "ssh": {"user": "pi"}}},
            "boards": [{"name": "b", "model": "m", "creds": {},
                        "drivers": {"console": {"type": "serial", "agent": "rpi"},
                                    "craft": {"type": "ip", "agent": "rpi"},
                                    "mgmt": {"type": "ip"},                       # ip, no agent
                                    "orphan": {"type": "serial", "agent": "gone"}}}]}
    board = inventory.parse(data, {}).boards[0]
    bench = inventory.parse(data, {})
    assert available_drivers(bench, board) == {"console", "craft", "mgmt"}   # orphan excluded (agent unknown)


def _host_spec(cid, verdict):
    from atf.core.model import CheckSpec, Result, Severity, Verdict
    return CheckSpec(id=cid, drivers=frozenset(), actions=frozenset(), severity=Severity.INFO,
                     title=cid, fn=lambda ctx: Result(getattr(Verdict, verdict), title="ok"))


def test_run_executes_host_check(example_bench, tmp_path):
    from atf.core import runner
    recs = runner.run(example_bench, [_host_spec("_t_ok", "PASS")], tmp_path, mgmt_backend="local")
    assert [r.verdict for r in recs if r.check == "_t_ok"] == ["pass"]


def test_run_skips_check_needing_absent_driver(example_bench, tmp_path):
    from atf.core import runner
    from atf.core.model import CheckSpec, Result, Severity, Verdict
    spec = CheckSpec(id="_t_needs", drivers=frozenset({"nope"}), actions=frozenset(),
                     severity=Severity.INFO, title="x", fn=lambda ctx: Result(Verdict.PASS))
    recs = runner.run(example_bench, [spec], tmp_path, mgmt_backend="local")
    assert [r.verdict for r in recs if r.check == "_t_needs"] == ["skipped"]   # unavailable driver → SKIP, not fail


def test_run_cancel_short_circuits(example_bench, tmp_path):
    from atf.core import runner
    ev = threading.Event()
    ev.set()                                    # cancel before anything runs
    recs = runner.run(example_bench, [_host_spec("_t_c", "PASS")], tmp_path, mgmt_backend="local", cancel=ev)
    assert recs == []


def test_resolve_selection_from_suite_map(discovered):
    from atf.core.registry import resolve_selection
    sel = {"requirements": [{"id": "acme:X", "tests": [{"id": "host-recon"}]}]}
    specs = resolve_selection(sel)
    assert [s.id for s in specs] == ["host-recon"]
