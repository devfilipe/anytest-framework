"""`atf remote …`: the CLI as a client of a running atf server. Each test runs the real web app in a
local uvicorn on a fresh store and drives it through `atf.cli.main`, the way an operator would from
another machine. The session (server URL + token, never the password) lives in ATF_CONFIG_DIR.
The `server` and `config_dir` fixtures are in conftest.py."""
from __future__ import annotations

import json
import os
import stat



def _login(server, monkeypatch, password="admin"):
    from atf.cli import main
    monkeypatch.setattr("getpass.getpass", lambda prompt="": password)
    return main(["remote", "login", "--server", server, "--user", "admin"])


def test_login_keeps_a_private_session_and_bench_get_reads_through_it(server, config_dir, monkeypatch, capsys):
    from atf.cli import main

    assert _login(server, monkeypatch) == 0
    session = config_dir / "session.json"
    assert stat.S_IMODE(os.stat(session).st_mode) == 0o600
    saved = json.loads(session.read_text())
    assert set(saved) == {"server", "user", "token"}           # never the password
    assert saved["server"] == server and saved["user"] == "admin" and saved["token"]
    capsys.readouterr()

    assert main(["remote", "bench", "get", "lab"]) == 0

    bench = json.loads(capsys.readouterr().out)
    assert bench["boards"][0]["drivers"]["mgmt"]["ip"] == "192.0.2.1"


def test_an_expired_session_says_to_log_in_again(server, config_dir, capsys):
    """The server keeps tokens in memory: after a restart the saved one is refused."""
    from atf.cli import main
    config_dir.mkdir()
    (config_dir / "session.json").write_text(json.dumps({"server": server, "user": "admin", "token": "stale"}))

    assert main(["remote", "bench", "get", "lab"]) == 1

    err = capsys.readouterr().err
    assert "session expired" in err and "atf remote login" in err
    assert "Traceback" not in err


def test_bench_put_writes_the_bench_from_a_yaml_file(server, config_dir, monkeypatch, capsys, tmp_path):
    from atf.cli import main
    _login(server, monkeypatch)
    f = tmp_path / "lab.yaml"
    f.write_text("agents: {}\nboards:\n- name: board-1\n  model: router-x\n  serial: '1'\n  creds: {}\n"
                 "  drivers:\n    mgmt: {driver_name: ip, ip: 192.0.2.1,\n"
                 "           restconf: {scheme: https, role: monitor, cert_ref: c, key_ref: k, ca_ref: ca}}\n")

    assert main(["remote", "bench", "put", "lab", str(f)]) == 0
    capsys.readouterr()
    assert main(["remote", "bench", "get", "lab"]) == 0

    mgmt = json.loads(capsys.readouterr().out)["boards"][0]["drivers"]["mgmt"]
    assert mgmt["restconf"] == {"scheme": "https", "role": "monitor", "cert_ref": "c", "key_ref": "k", "ca_ref": "ca"}


def test_secret_set_stores_the_file_content_and_never_prints_it(server, config_dir, monkeypatch, capsys,
                                                               tmp_path, store):
    from atf.cli import main
    _login(server, monkeypatch)
    pem = "-----BEGIN PRIVATE KEY-----\nMIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEH\n-----END PRIVATE KEY-----\n"
    f = tmp_path / "client.key"
    f.write_text(pem)
    capsys.readouterr()

    assert main(["remote", "secret", "set", "lab", "k", "--file", str(f)]) == 0

    out = capsys.readouterr()
    assert "MIGHAgEA" not in out.out + out.err
    assert store.secrets("lab", reveal=True)["k"] == pem


def test_bench_list_names_the_benches_and_logout_forgets_the_session(server, config_dir, monkeypatch, capsys):
    from atf.cli import main
    _login(server, monkeypatch)
    capsys.readouterr()

    assert main(["remote", "bench", "list"]) == 0
    assert "lab" in capsys.readouterr().out.split()

    assert main(["remote", "logout"]) == 0
    assert not (config_dir / "session.json").exists()
    assert main(["remote", "bench", "list"]) == 1
    assert "atf remote login" in capsys.readouterr().err


def test_driver_set_changes_one_driver_and_keeps_the_rest_of_the_bench(server, config_dir, monkeypatch, capsys):
    from atf.cli import main
    _login(server, monkeypatch)

    assert main(["remote", "driver", "set", "lab", "board-1", "mgmt", "--prop", "ip=192.0.2.9",
                 "--restconf", "scheme=https", "--restconf", "role=monitor", "--restconf", "port=8443",
                 "--restconf", "cert_ref=c", "--restconf", "key_ref=k", "--restconf", "ca_ref=ca"]) == 0
    capsys.readouterr()
    main(["remote", "bench", "get", "lab"])
    board = json.loads(capsys.readouterr().out)["boards"][0]

    assert board["drivers"]["mgmt"]["ip"] == "192.0.2.9"
    assert board["drivers"]["mgmt"]["restconf"] == {"scheme": "https", "role": "monitor", "port": 8443,
                                                    "cert_ref": "c", "key_ref": "k", "ca_ref": "ca"}
    assert board["name"] == "board-1" and board["model"] == "router-x"

    assert main(["remote", "driver", "set", "lab", "board-1", "mgmt", "--no-restconf"]) == 0
    capsys.readouterr()
    main(["remote", "bench", "get", "lab"])
    assert "restconf" not in json.loads(capsys.readouterr().out)["boards"][0]["drivers"]["mgmt"]


def test_driver_set_on_an_unknown_board_is_an_error(server, config_dir, monkeypatch, capsys):
    from atf.cli import main
    _login(server, monkeypatch)
    capsys.readouterr()

    assert main(["remote", "driver", "set", "lab", "board-x", "mgmt", "--prop", "ip=192.0.2.9"]) == 1
    assert "board-x" in capsys.readouterr().err


def _local_bench(store):
    store.upsert_bench("loop", {"agents": {}, "boards": [
        {"name": "board-1", "model": "", "serial": "1", "creds": {},
         "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1"}}}]})


def test_run_waits_and_report_download_writes_the_run_folder(server, config_dir, monkeypatch, capsys, store, tmp_path):
    from atf.cli import main
    _local_bench(store)
    _login(server, monkeypatch)
    capsys.readouterr()

    assert main(["remote", "run", "--bench", "loop", "--board", "board-1", "--id", "host-recon", "--wait"]) == 0
    out = capsys.readouterr().out
    run_id = next(w for w in out.split() if w.startswith("run-"))
    assert "host-recon" in out

    dest = tmp_path / "run"
    assert main(["remote", "report", "download", run_id, "--out", str(dest)]) == 0
    records = json.loads((dest / "results.json").read_text())
    assert [r["check"] for r in records] == ["host-recon"]
    assert json.loads((dest / "run-meta.json").read_text())["run_id"] == run_id
    assert any((dest / "evidence").iterdir())


def test_a_run_that_fails_to_start_says_why(server, config_dir, monkeypatch, capsys):
    from atf.cli import main
    _login(server, monkeypatch)
    capsys.readouterr()

    assert main(["remote", "run", "--bench", "no-such-bench", "--id", "host-recon", "--wait"]) == 1
    assert "no-such-bench" in capsys.readouterr().err
