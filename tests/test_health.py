"""Bench health: per board and driver, whether the server reaches what the driver points at — a
network probe only (ICMP + TCP connect), never a login. `GET /api/benches/health`."""
from __future__ import annotations

import shutil
import socket

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ping") is None, reason="needs ping on the host")


@pytest.fixture
def listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    yield s.getsockname()[1]
    s.close()


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def fast_probes(monkeypatch):
    monkeypatch.setenv("ATF_HEALTH_TIMEOUT", "0.5")


def test_each_driver_reports_ok_degraded_or_down(client, store, listener):
    closed = _closed_port()
    store.upsert_bench("hb", {"agents": {"node-1": {"platform": "linux", "host": "127.0.0.1",
                                                     "ssh": {"user": "u", "port": listener}}},
                              "boards": [{"name": "board-1", "model": "router-x", "serial": "1", "creds": {}, "drivers": {
                                  "mgmt": {"driver_name": "ip", "ip": "127.0.0.1",
                                           "restconf": {"scheme": "https", "port": listener}},
                                  "oob": {"driver_name": "ip", "ip": "127.0.0.1", "restconf": {"port": closed}},
                                  "lost": {"driver_name": "ip", "ip": "192.0.2.1"},
                                  "console": {"driver_name": "serial", "agent": "node-1", "device": "/dev/ttyUSB0"}}}]})

    health = client.get("/api/benches/health").json()["hb"]["board-1"]

    assert health["mgmt"]["status"] == "ok" and health["mgmt"]["target"] == f"127.0.0.1:{listener}"
    assert health["oob"]["status"] == "degraded" and "port" in health["oob"]["reason"]
    assert health["lost"]["status"] == "down"
    assert health["console"]["status"] == "ok" and health["console"]["target"] == f"node-1 127.0.0.1:{listener}"


def test_health_is_cached_until_refreshed(client, store, listener):
    bench = {"agents": {}, "boards": [{"name": "board-1", "model": "router-x", "serial": "1", "creds": {},
                                       "drivers": {"mgmt": {"driver_name": "ip", "ip": "127.0.0.1",
                                                            "restconf": {"port": listener}}}}]}
    store.upsert_bench("hb", bench)
    first = client.get("/api/benches/health").json()["hb"]["board-1"]["mgmt"]
    bench["boards"][0]["drivers"]["mgmt"]["restconf"]["port"] = _closed_port()
    store.upsert_bench("hb", bench)

    cached = client.get("/api/benches/health").json()["hb"]["board-1"]["mgmt"]
    fresh = client.get("/api/benches/health?refresh=1").json()["hb"]["board-1"]["mgmt"]

    assert first["status"] == cached["status"] == "ok"
    assert fresh["status"] == "degraded"
