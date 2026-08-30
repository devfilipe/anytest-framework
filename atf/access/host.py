"""Probes from the operator host's own vantage (no board channel)."""
from __future__ import annotations

import socket
import subprocess


class HostProbe:
    def ping(self, ip: str, timeout: int = 2) -> bool:
        try:
            r = subprocess.run(
                ["ping", "-c", "1", "-W", str(timeout), ip],
                capture_output=True,
            )
            return r.returncode == 0
        except FileNotFoundError:
            return False

    def tcp(self, ip: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                return True
        except OSError:
            return False
