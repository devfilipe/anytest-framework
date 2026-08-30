"""IpChannel — driver type "ip": reach a board over the network at ``config["ip"]``.

Unifies the old craft + mgmt vantages, chosen by whether the driver has an **agent**:
  - **with agent**  → probe from that agent's L2 vantage (the old craft: what a laptop on the
    segment sees) via ``AgentConn.ping``/``tcp_scan``.
  - **without agent** → probe from the host / the atf-mgmt container (the old mgmt): ``nmap`` for
    scan/NSE and paramiko SSH for a board shell, using the bench-provided creds.

The driver object carries its target address as ``ctx.<alias>.ip`` and exposes ping/tcp/scan/nse/sh.
"""
from __future__ import annotations

import socket
import subprocess

from atf.access.channels.base import Channel, CmdResult

DEFAULT_PORTS = (22, 80, 443, 830, 4565, 6379, 8080)
DEFAULT_NMAP_PORTS = "1-1024,4565,6379,8080"


def _parse_nmap_grep(text: str) -> list[int]:
    ports: list[int] = []
    for line in text.splitlines():
        if "Ports:" not in line:
            continue
        for item in line.split("Ports:", 1)[1].split(","):
            f = item.strip().split("/")
            if len(f) >= 2 and f[1] == "open":
                try:
                    ports.append(int(f[0]))
                except ValueError:
                    pass
    return sorted(set(ports))


class IpChannel(Channel):
    type = "ip"

    def __init__(self, config, agent_conn=None, creds=None, role: str = "root", connect_timeout: int = 15):
        self.cfg = config or {}
        self.ip = self.cfg.get("ip")
        self.ac = agent_conn                 # None → probe from host / container
        self.creds = creds or {}
        self._role = role
        self._ct = connect_timeout
        self._cli = None

    # --- reachability / scan (target defaults to this driver's own ip) ---
    def ping(self, target: str | None = None) -> bool:
        target = target or self.ip
        if self.ac is not None:
            return self.ac.ping(target)
        try:
            return subprocess.run(["ping", "-c", "1", "-W", "2", target],
                                  capture_output=True).returncode == 0
        except FileNotFoundError:
            return False

    def tcp(self, host: str | None = None, port: int = 0) -> bool:
        host = host or self.ip
        if self.ac is not None and hasattr(self.ac, "tcp_scan"):
            return bool(self.ac.tcp_scan(host, [port]))
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            return False

    def scan(self, target: str | None = None, ports=None) -> list[int]:
        target = target or self.ip
        if self.ac is not None:
            return self.ac.tcp_scan(target, tuple(ports) if ports else DEFAULT_PORTS)
        p = ports if isinstance(ports, str) else (
            ",".join(str(x) for x in ports) if ports else DEFAULT_NMAP_PORTS)
        r = subprocess.run(["nmap", "-Pn", "-sT", "--open", "-oG", "-", "-T4", "-p", p, target],
                           capture_output=True, text=True, timeout=900)
        return _parse_nmap_grep(r.stdout)

    def nse(self, target: str | None = None, ports="443", scripts: str = "ssl-enum-ciphers",
            timeout: int = 900) -> str:
        """Raw nmap NSE output (host/container vantage) — e.g. ssl-enum-ciphers for a TLS audit."""
        target = target or self.ip
        r = subprocess.run(["nmap", "-Pn", "-sT", "-T4", "--script", scripts, "-p", str(ports),
                            "-oN", "-", target], capture_output=True, text=True, timeout=timeout)
        return r.stdout

    # --- board shell over SSH (no-agent ip vantage), using the bench creds ---
    def _ssh(self):
        if self._cli is None:
            import paramiko
            cr = self.creds[self._role]
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(self.ip, username=cr.user, password=cr.password,
                      timeout=self._ct, allow_agent=False, look_for_keys=False)
            self._cli = c
        return self._cli

    def sh(self, cmd: str, timeout: int = 30) -> CmdResult:
        _in, out, err = self._ssh().exec_command(cmd, timeout=timeout)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        return CmdResult(out.channel.recv_exit_status(), o, e)

    def close(self) -> None:
        if self._cli is not None:
            try:
                self._cli.close()
            finally:
                self._cli = None
        if self.ac is not None:
            self.ac.close()
