"""Reservations: a lock on a board that outlives one run, so a sequence of runs keeps the board.
`atf remote lock take` reserves, `atf remote run --reservation` runs under it, `atf remote lock
release` frees it, and it expires by itself. The server is the real app in a local uvicorn
(conftest `server`); the bench points at 127.0.0.1."""
from __future__ import annotations

import time

import pytest


def _login(server, monkeypatch, user="admin", password="admin"):
    from atf.cli import main
    monkeypatch.setattr("getpass.getpass", lambda prompt="": password)
    assert main(["remote", "login", "--server", server, "--user", user]) == 0


@pytest.fixture
def loop(store):
    store.upsert_bench("loop", {"agents": {}, "boards": [
        {"name": "board-1", "model": "", "serial": "1", "creds": {},
         "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1"}}},
        {"name": "board-2", "model": "", "serial": "2", "creds": {},
         "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1"}}}]})
    return "loop"


def _take(capsys, *args):
    from atf.cli import main
    capsys.readouterr()
    assert main(["remote", "lock", "take", *args]) == 0
    return capsys.readouterr().out.split()[0]


def _run(capsys, *extra):
    from atf.cli import main
    capsys.readouterr()
    rc = main(["remote", "run", "--bench", "loop", "--board", "board-1", "--id", "host-recon", "--wait", *extra])
    return rc, capsys.readouterr()


def test_runs_under_a_reservation_keep_the_board_and_others_find_it_busy(server, config_dir, monkeypatch,
                                                                       capsys, loop):
    from atf.cli import main
    _login(server, monkeypatch)
    rid = _take(capsys, "loop", "--board", "board-1", "--ttl", "120")

    rc, out = _run(capsys)                                       # no reservation: the board is taken
    assert rc == 1 and "bench busy" in out.err and "board:board-1" in out.err and rid in out.err

    for _ in range(2):                                           # several runs reuse it
        rc, out = _run(capsys, "--reservation", rid)
        assert rc == 0, out.err
        assert "host-recon" in out.out

    rc, out = _run(capsys)                                       # still held after those runs ended
    assert rc == 1 and "bench busy" in out.err

    assert main(["remote", "lock", "release", rid]) == 0
    rc, out = _run(capsys)
    assert rc == 0, out.err


def test_lock_list_shows_the_reservation_with_its_holder_and_expiry(server, config_dir, monkeypatch, capsys, loop):
    from atf.cli import main
    _login(server, monkeypatch)
    rid = _take(capsys, "loop", "--board", "board-1", "--ttl", "120")

    assert main(["remote", "lock", "list"]) == 0
    out = capsys.readouterr().out
    [line] = [ln for ln in out.splitlines() if "board:board-1" in ln]
    assert rid in line and "admin" in line and "expires in" in line
    assert "board:board-2" not in out

    main(["remote", "lock", "release", rid])
    capsys.readouterr()
    assert main(["remote", "lock", "list"]) == 0
    assert "board:board-1" not in capsys.readouterr().out


def test_a_reservation_expires_by_itself(server, config_dir, monkeypatch, capsys, loop):
    _login(server, monkeypatch)
    rid = _take(capsys, "loop", "--board", "board-1", "--ttl", "1")
    time.sleep(1.3)

    rc, out = _run(capsys)
    assert rc == 0, out.err

    rc, out = _run(capsys, "--reservation", rid)
    assert rc == 1 and rid in out.err and "expired" in out.err


def test_taking_a_board_already_reserved_says_who_holds_it(server, config_dir, monkeypatch, capsys, loop):
    from atf.cli import main
    _login(server, monkeypatch)
    rid = _take(capsys, "loop", "--board", "board-1", "--ttl", "120")

    assert main(["remote", "lock", "take", "loop", "--board", "board-1", "--board", "board-2", "--ttl", "60"]) == 1
    err = capsys.readouterr().err
    assert "busy" in err and rid in err
    assert main(["remote", "lock", "list"]) == 0
    assert "board:board-2" not in capsys.readouterr().out          # all or nothing


def test_a_reservation_on_an_unknown_board_or_with_a_bad_ttl_is_refused(client, loop):
    assert client.post("/api/locks/reservations", json={"bench": "loop", "board": ["board-9"], "ttl": 60}).status_code == 404
    assert client.post("/api/locks/reservations", json={"bench": "nope", "board": ["board-1"], "ttl": 60}).status_code == 404
    for ttl in (0, -5, 10 ** 9, "x"):
        assert client.post("/api/locks/reservations",
                           json={"bench": "loop", "board": ["board-1"], "ttl": ttl}).status_code == 400


def test_another_user_can_neither_run_under_nor_release_my_reservation(client, loop):
    rid = client.post("/api/locks/reservations", json={"bench": "loop", "board": ["board-1"], "ttl": 60}).json()["id"]
    client.post("/api/users", json={"username": "bob", "password": "bob-pass", "is_admin": False})
    bob = {"Authorization": "Bearer " + client.post("/api/admin/login",
                                                    json={"user": "bob", "password": "bob-pass"}).json()["token"]}

    assert client.delete(f"/api/locks/reservations/{rid}", headers=bob).status_code == 403
    assert client.post("/api/run", headers=bob, json={"bench": "loop", "board": ["board-1"], "ids": ["host-recon"],
                                                      "reservation": rid}).json()["started"] is True
    deadline = time.time() + 30
    while client.get("/api/run/status").json()["active"] and time.time() < deadline:
        time.sleep(0.1)
    last = client.get("/api/run/status").json()["last"]
    assert last["type"] == "error" and rid in last["message"] and "not yours" in last["message"]
    assert client.delete(f"/api/locks/reservations/{rid}").json() == {"ok": True}   # the owner can
    assert client.delete(f"/api/locks/reservations/{rid}").status_code == 404


def test_a_reservation_that_runs_out_during_a_run_holds_until_that_run_ends():
    """Expiry never pulls the board from under a run: the reservation goes when its last run ends."""
    from atf.web.locks import ResourceLocks
    now = [1000.0]
    locks = ResourceLocks(clock=lambda: now[0])
    ok, rid = locks.reserve({"board:b1"}, owner="alice", ttl=10)
    assert ok
    assert locks.acquire({"board:b1"}, "run-1", who="pilot", reservation=rid, owner="alice") == (True, {})

    now[0] += 60                                                  # past the TTL, run-1 still going
    ok, conflict = locks.acquire({"board:b1"}, "run-2", who="pilot")
    assert not ok and conflict["board:b1"]["run"] == rid

    locks.release("run-1")                                        # its last run ends: the expiry applies
    assert locks.acquire({"board:b1"}, "run-2", who="pilot") == (True, {})


def test_releasing_a_reservation_during_its_run_frees_the_board_when_that_run_ends():
    from atf.web.locks import ReservationError, ResourceLocks
    locks = ResourceLocks()
    _, rid = locks.reserve({"board:b1"}, owner="alice", ttl=600)
    locks.acquire({"board:b1"}, "run-1", reservation=rid, owner="alice")

    assert locks.unreserve(rid) is True
    with pytest.raises(ReservationError):                         # no new run under it
        locks.acquire({"board:b1"}, "run-2", reservation=rid, owner="alice")
    assert locks.acquire({"board:b1"}, "run-3")[0] is False         # run-1 still has the board

    locks.release("run-1")
    assert locks.acquire({"board:b1"}, "run-3") == (True, {})
