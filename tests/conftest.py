"""Shared fixtures. Each test gets an isolated SQLite store; the API tests get a logged-in
TestClient. The bundled `examples/` set is the check-source for discovery."""
from __future__ import annotations

import os
import pathlib
import socket
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"


@pytest.fixture
def store(tmp_path):
    """A fresh, migrated config store (schema applied, admin seeded, built-in entities present)."""
    from atf.store import open_repo
    repo = open_repo(db_path=str(tmp_path / "store.db"), app_secret="test-secret")
    repo.ensure_admin()
    return repo


@pytest.fixture(scope="session")
def discovered():
    """Discover the bundled example checks into the registry (host-recon)."""
    os.environ["ATF_CHECK_SOURCES"] = str(EXAMPLES)
    from atf.core import checks
    checks.discover()
    return True


@pytest.fixture
def client(store, discovered, tmp_path):
    """A FastAPI TestClient on the fresh store, logged in as admin."""
    from fastapi.testclient import TestClient

    from atf.web.server import build_app
    app = build_app(tmp_path / "out", str(EXAMPLES / "benches" / "lab.yaml"), store)
    c = TestClient(app)
    tok = c.post("/api/admin/login", json={"user": "admin", "password": "admin"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


@pytest.fixture
def example_bench():
    """The parsed example bench (one board, a mgmt ip driver at 127.0.0.1)."""
    from atf.core import inventory
    return inventory.load(str(EXAMPLES / "benches" / "lab.yaml"))


@pytest.fixture
def server(store, discovered, tmp_path):
    """The atf web app served over HTTP on 127.0.0.1, backed by the fresh store (admin/admin)."""
    import uvicorn

    from atf.web.server import build_app
    store.upsert_bench("lab", {"agents": {}, "boards": [
        {"name": "board-1", "model": "router-x", "serial": "1", "creds": {},
         "drivers": {"mgmt": {"driver_name": "ip", "ip": "192.0.2.1"}}}]})
    app = build_app(tmp_path / "out", str(EXAMPLES / "benches" / "lab.yaml"), store)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=srv.run, daemon=True).start()
    deadline = time.time() + 10
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    d = tmp_path / "atf-config"
    monkeypatch.setenv("ATF_CONFIG_DIR", str(d))
    return d
