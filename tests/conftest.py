"""Shared fixtures. Each test gets an isolated SQLite store; the API tests get a logged-in
TestClient. The bundled `examples/` set is the check-source for discovery."""
from __future__ import annotations

import os
import pathlib

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
