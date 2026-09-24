"""Probes from the operator host's own vantage (no board channel)."""
from __future__ import annotations

import shutil
import socket
import subprocess


def icmp_ping(ip: str, timeout: int = 2) -> bool:
    """One ICMP echo to `ip` from THIS vantage (the host, or the atf-mgmt container).

    Falls back to `nmap -sn` where the vantage ships a scanner but no ping binary, and RAISES when
    it has neither: a missing probe tool is not the same as an unreachable board, and answering a
    silent False reports a live board as down.
    """
    if shutil.which("ping"):
        try:
            return subprocess.run(["ping", "-c", "1", "-W", str(timeout), ip],
                                  capture_output=True, timeout=timeout + 3).returncode == 0
        except subprocess.TimeoutExpired:
            return False
    if shutil.which("nmap"):                      # the mgmt image's scanner does ICMP host discovery
        try:
            r = subprocess.run(["nmap", "-sn", "-n", "--host-timeout", f"{timeout}s", ip],
                               capture_output=True, text=True, timeout=timeout + 8)
        except subprocess.TimeoutExpired:
            return False
        return "Host is up" in r.stdout
    raise RuntimeError(f"no ICMP probe available in this vantage (cannot tell if {ip} is up): "
                       "install iputils-ping (or nmap)")


class HostProbe:
    def ping(self, ip: str, timeout: int = 2) -> bool:
        return icmp_ping(ip, timeout)

    def tcp(self, ip: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False
