"""Run parameters: a run carries a `params` dict that every check of the run reads as `ctx.params`,
in-process, in the mgmt worker and in the isolated worker of an agent's tree, and that run-meta
records. The targets are local (127.0.0.1); no board is involved."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

ECHO = '''
from atf.core.model import Result, Verdict
from atf.core.registry import register


@register(id="agent-params-echo", title="echoes the run parameters")
def _echo(ctx):
    return Result(Verdict.PASS, metrics={"params": dict(ctx.params)})
'''


@pytest.fixture
def echo_checks():
    """Two checks that echo ctx.params: one in-process (no driver), one in the mgmt worker."""
    from atf.core.model import Result, Verdict
    from atf.core.registry import REGISTRY, register

    @register(id="test-params-host", title="echoes the run parameters in-process")
    def _host(ctx):
        return Result(Verdict.PASS, metrics={"params": dict(ctx.params)})

    @register(id="test-params-mgmt", drivers=("mgmt",), title="echoes the run parameters in the mgmt worker")
    def _mgmt(ctx):
        return Result(Verdict.PASS, metrics={"params": dict(ctx.params)})

    yield ["test-params-host", "test-params-mgmt"]
    REGISTRY.pop("test-params-host", None)
    REGISTRY.pop("test-params-mgmt", None)


def _bench():
    from atf.core.inventory import parse
    return parse({"boards": [{"name": "board-1", "drivers": {"mgmt": {"type": "ip", "ip": "127.0.0.1"}}}]}, {})


def test_every_check_of_a_run_reads_the_params_and_run_meta_records_them(echo_checks, discovered, tmp_path):
    from atf.core import runner
    from atf.core.registry import REGISTRY

    recs = runner.run(_bench(), [REGISTRY[i] for i in echo_checks], tmp_path, mgmt_backend="local",
                      params={"phase": "factory", "tag": "v1.2"})

    assert {r.check: (r.verdict, r.metrics["params"]) for r in recs} == {
        "test-params-host": ("pass", {"phase": "factory", "tag": "v1.2"}),
        "test-params-mgmt": ("pass", {"phase": "factory", "tag": "v1.2"})}
    assert json.loads((tmp_path / "run-meta.json").read_text())["params"] == {"phase": "factory", "tag": "v1.2"}


def test_a_run_without_params_gives_the_checks_an_empty_dict(echo_checks, discovered, tmp_path):
    from atf.core import runner
    from atf.core.registry import REGISTRY

    recs = runner.run(_bench(), [REGISTRY[i] for i in echo_checks], tmp_path, mgmt_backend="local")

    assert [r.metrics["params"] for r in recs] == [{}, {}]
    assert json.loads((tmp_path / "run-meta.json").read_text())["params"] == {}


def _login(server, monkeypatch):
    from atf.cli import main
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "admin")
    assert main(["remote", "login", "--server", server, "--user", "admin"]) == 0


def test_remote_run_passes_params_to_the_checks_and_the_run_folder(server, config_dir, monkeypatch, capsys,
                                                                   store, tmp_path, echo_checks):
    from atf.cli import main
    store.upsert_bench("loop", {"agents": {}, "boards": [
        {"name": "board-1", "model": "", "serial": "1", "creds": {},
         "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1"}}}]})
    _login(server, monkeypatch)
    capsys.readouterr()

    assert main(["remote", "run", "--bench", "loop", "--id", "test-params-host", "--id", "test-params-mgmt",
                 "--param", "phase=factory", "--param", "url=http://192.0.2.9/a=b", "--wait"]) == 0
    run_id = next(w for w in capsys.readouterr().out.split() if w.startswith("run-"))
    dest = tmp_path / "run"
    assert main(["remote", "report", "download", run_id, "--out", str(dest)]) == 0

    want = {"phase": "factory", "url": "http://192.0.2.9/a=b"}      # values stay strings; only the first = splits
    assert [r["metrics"]["params"] for r in json.loads((dest / "results.json").read_text())] == [want, want]
    assert json.loads((dest / "run-meta.json").read_text())["params"] == want


def test_remote_run_refuses_a_param_without_a_value(server, config_dir, monkeypatch, capsys):
    from atf.cli import main
    _login(server, monkeypatch)
    capsys.readouterr()

    assert main(["remote", "run", "--bench", "lab", "--id", "host-recon", "--param", "phase"]) == 1
    assert "key=value" in capsys.readouterr().err


def test_the_api_refuses_params_that_are_not_a_mapping(client):
    r = client.post("/api/run", json={"bench": "lab", "ids": ["host-recon"], "params": ["phase=factory"]})

    assert r.json()["started"] is False
    assert "params" in r.json()["error"]


def test_an_agent_tree_run_reads_the_params(tmp_path, monkeypatch, store):
    """The server runs an agent's uploaded tree in `atf _agent-worker`; the params reach its checks."""
    from atf.web.server import _agent_worker_run

    monkeypatch.setenv("DATABASE_URL", str(tmp_path / "store.db"))   # the store of the `store` fixture
    monkeypatch.setenv("ATF_WORK", str(tmp_path / "work"))
    store.upsert_bench("loop", {"agents": {}, "boards": [
        {"name": "board-1", "model": "", "serial": "1", "creds": {},
         "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1"}}}]})
    tree = tmp_path / "tree"
    model_dir = tree / "dev-checks" / "atf_checks" / "router_x"     # a model segment: `common` is the examples'
    model_dir.mkdir(parents=True)
    (model_dir / "__init__.py").write_text("")
    (model_dir / "params_echo.py").write_text(ECHO)
    ev = threading.Event()
    ev.set()
    hub = SimpleNamespace(request_tree=lambda s: ("t", ev))
    agent = SimpleNamespace(uploads={"t": {"dir": str(tree)}})

    recs, _dev, out, meta = _agent_worker_run(hub, agent, "loop", "board-1", {"ids": ["agent-params-echo"]},
                                              "local", params={"phase": "configured"})

    assert [(r["check"], r["metrics"]["params"]) for r in recs] == [("agent-params-echo", {"phase": "configured"})]
    assert meta["params"] == {"phase": "configured"}
