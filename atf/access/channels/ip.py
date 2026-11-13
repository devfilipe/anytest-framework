"""IpChannel — driver type "ip": reach a board over the network at ``config["ip"]``.

Unifies the old craft + mgmt vantages, chosen by whether the driver has an **agent**:
  - **with agent**  → probe from that agent's L2 vantage (the old craft: what a host on the
    segment sees) via ``AgentConn.ping``/``tcp_scan``.
  - **without agent** → probe from the host / the atf-mgmt container (the old mgmt): ``nmap`` for
    scan/NSE and paramiko SSH for a board shell, using the bench-provided creds.

The driver object carries its target address as ``ctx.<alias>.ip`` and exposes ping/tcp/scan/nse/sh,
and ``restconf_get``/``restconf_request`` when the driver config has a ``restconf`` block.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request

from atf.access.channels.base import Channel, CmdResult
from atf.access.host import icmp_ping

DEFAULT_PORTS = (22, 80, 443, 830, 4565, 6379, 8080)
DEFAULT_NMAP_PORTS = "1-1024,4565,6379,8080"
RESTCONF_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


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

    def __init__(self, config, agent_conn=None, creds=None, role: str = "root", connect_timeout: int = 15,
                 tls: dict | None = None):
        self.cfg = config or {}
        self.ip = self.cfg.get("ip")
        self.ac = agent_conn                 # None → probe from host / container
        self.creds = creds or {}
        self._tls = tls or {}                # {cert, key, ca} PEM text for the restconf client
        self._role = role
        self._ct = connect_timeout
        self._cli = None

    # --- reachability / scan (target defaults to this driver's own ip) ---
    def ping(self, target: str | None = None) -> bool:
        target = target or self.ip
        if self.ac is not None:
            return self.ac.ping(target)
        return icmp_ping(target)          # host/container vantage — raises if it has no probe tool

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

    # --- RESTCONF (RFC 8040), configured by the driver's `restconf` block ---
    def _restconf_ssl(self, client_cert: bool = True) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cadata=self._tls["ca"]) if self._tls.get("ca") else ssl.create_default_context()
        if client_cert and self._tls.get("cert") and self._tls.get("key"):
            # load_cert_chain reads files only: the PEM text lives in a private temp dir for the call
            with tempfile.TemporaryDirectory(prefix="atf-tls-") as d:
                cert, key = os.path.join(d, "cert.pem"), os.path.join(d, "key.pem")
                for path, text in ((cert, self._tls["cert"]), (key, self._tls["key"])):
                    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
                        f.write(text)
                ctx.load_cert_chain(cert, key)
        return ctx

    def _restconf(self, method: str, path: str, body=None, auth=None, timeout: int = 30) -> tuple[int, str]:
        method = method.upper()
        if method not in RESTCONF_METHODS:
            raise ValueError(f"RESTCONF has no method {method} (use one of {', '.join(RESTCONF_METHODS)})")
        rc = self.cfg.get("restconf") or {}
        scheme = rc.get("scheme", "https")
        port = rc.get("port") or (443 if scheme == "https" else 80)
        headers = {"Accept": "application/yang-data+json"}
        client_cert = auth is None
        if auth is None:
            cr = self.creds.get(rc.get("role", self._role))
            if cr is not None and cr.password and not self._tls.get("cert"):
                auth = (cr.user, cr.password)    # no client certificate: the role's credentials (Basic, RFC 7617)
        if auth:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/yang-data+json"
        url = f"{scheme}://{self.ip}:{port}" + (path if path.startswith("/") else f"/restconf/data/{path}")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        ctx = self._restconf_ssl(client_cert) if scheme == "https" else None
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:                       # an HTTPError holds the open response: close it
                status, raw = e.code, e.read()
        return status, raw.decode("utf-8", "replace")

    def restconf_request(self, method: str, path: str, body=None, auth=None,
                         timeout: int = 30) -> tuple[int, object]:
        """A RESTCONF request: ``method`` is GET/POST/PUT/PATCH/DELETE, ``path`` is under
        ``/restconf/data/`` (or from the server root when it starts with ``/``, e.g.
        ``/restconf/operations/<rpc>``), ``body`` is sent as JSON (``application/yang-data+json``).

        Authentication follows ``restconf_get``: the bench's client certificate over https, else the
        restconf role's password (Basic). ``auth=(user, password)`` sends those credentials instead
        and leaves the client certificate out, so the server sees only that identity;
        ``auth=False`` sends no credentials at all.

        Returns (HTTP status, parsed JSON body — the raw text when it is not JSON, None when it is
        empty). An HTTP error status is returned, not raised; a transport failure raises."""
        status, text = self._restconf(method, path, body, auth, timeout)
        if not text:
            return status, None
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def restconf_get(self, path: str, timeout: int = 30) -> tuple[int, object]:
        """GET ``/restconf/data/<path>``; returns (HTTP status, parsed JSON body — or the raw text when
        it is not JSON). An HTTP error status is returned, not raised; a transport failure raises."""
        status, text = self._restconf("GET", path, timeout=timeout)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

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
