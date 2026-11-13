"""Bench health: whether the server reaches what each driver of each board points at.

A network probe only — ICMP and a TCP connect, never a login — so it can run whenever the Benches
page opens without touching a board's sessions or logs:

- an `ip` driver without an agent: the target port (the `restconf` port when the driver has one,
  else 443/80 by its scheme, else SSH 22) on the driver's ip;
- an `ip` driver with an agent, and a `serial` driver: the agent's SSH, since the probe runs there.

`ok` when the port answers; `degraded` when only ping does; `down` otherwise.
"""
from __future__ import annotations

import os
import socket
from concurrent.futures import ThreadPoolExecutor

from atf.access.host import icmp_ping
from atf.core.inventory import Bench


def _timeout() -> float:
    try:
        return float(os.environ.get("ATF_HEALTH_TIMEOUT", "3"))
    except ValueError:
        return 3.0


def _tcp(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=_timeout()):
            return True
    except OSError:
        return False


def _ping(host: str) -> bool | None:
    """ICMP reachability; None when this host has no probe tool (not the same as down)."""
    try:
        return bool(icmp_ping(host))
    except Exception:  # noqa: BLE001 — a missing tool or odd target degrades to "unknown"
        return None


def _target(bench: Bench, cfg: dict) -> tuple[str, str, int] | None:
    """(label, host, port) a driver's health is probed on, or None when it points at nothing."""
    agent = bench.agents.get(cfg.get("agent") or "")
    if cfg.get("type", cfg.get("driver_name")) == "serial" or agent is not None:
        if agent is None or not agent.host:
            return None
        return f"{agent.name} {agent.host}:{agent.ssh_port}", agent.host, agent.ssh_port
    ip = cfg.get("ip")
    if not ip:
        return None
    rc = cfg.get("restconf") if isinstance(cfg.get("restconf"), dict) else None
    port = (rc or {}).get("port") or ({"https": 443, "http": 80}.get((rc or {}).get("scheme", "https")) if rc else 22)
    return f"{ip}:{port}", ip, int(port)


def _probe(bench: Bench, cfg: dict) -> dict:
    t = _target(bench, cfg)
    if t is None:
        return {"status": "down", "target": "", "reason": "no address or agent configured"}
    label, host, port = t
    if _tcp(host, port):
        return {"status": "ok", "target": label, "reason": f"port {port} answers"}
    if _ping(host):
        return {"status": "degraded", "target": label, "reason": f"host answers ping, port {port} does not"}
    return {"status": "down", "target": label, "reason": f"no answer from {host} (ping, port {port})"}


def bench_health(bench: Bench) -> dict[str, dict[str, dict]]:
    """{board: {driver alias: {status, target, reason}}}, the drivers probed in parallel."""
    jobs = [(b.name, alias, cfg) for b in bench.boards for alias, cfg in (b.drivers or {}).items()
            if isinstance(cfg, dict)]
    out: dict[str, dict[str, dict]] = {b.name: {} for b in bench.boards}
    with ThreadPoolExecutor(max_workers=max(1, min(16, len(jobs)))) as pool:
        for (board, alias, _), result in zip(jobs, pool.map(lambda j: _probe(bench, j[2]), jobs)):
            out[board][alias] = result
    return out
