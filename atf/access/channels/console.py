"""SerialChannel — driver type "serial": a board serial console via an agent.

send/expect/login/sh over the serial line. The OS-specific serial bridge lives in
AgentConn.serial_stream (socat on linux, plink on windows), so it can be driven from a Pi or a
notebook alike. Transport per driver config: `ssh` (bridge over the agent) or `ser2net` (raw TCP
socket to the agent). The typical alias is `console`; it is the only driver on an IP-less board.
"""
from __future__ import annotations

import re
import shlex
import socket
import time

from atf.access.channels.base import Channel, CmdResult

_PROMPT = r"[#%$]\s*$"          # bash '#'/'$' or a device shell '%'


class SerialChannel(Channel):
    type = "serial"

    def __init__(self, agent_conn, config):
        self.ac = agent_conn
        self.b = config or {}
        self._t = None
        self._buf = ""

    def _open(self):
        if self._t is not None:
            return
        if (self.b.get("transport") or "ssh") == "ser2net":
            from atf.access.agent import _StreamT
            s = socket.create_connection((self.ac.agent.host, int(self.b["port"])), timeout=15)
            self._t = _StreamT(s, is_socket=True)
        else:
            serial = self.b.get("serial") or {}       # legacy nested {dev,baud}
            dev = self.b.get("device") or serial.get("dev") or "/dev/ttyUSB0"
            baud = self.b.get("baud") or serial.get("baud") or 115200
            self._free_tty(dev)
            self._t = self.ac.serial_stream(dev, int(baud))

    def _free_tty(self, dev: str) -> None:
        """Console needs EXCLUSIVE access. A leftover picocom/minicom/screen holding the
        tty blocks the socat bridge silently (opens but reads 0 bytes → expect timeout).
        Kill any holder on the agent first. Best-effort; linux agents only (fuser)."""
        if self.ac.platform == "windows":
            return
        try:
            self.ac.run(f"fuser -k {shlex.quote(dev)}")
            time.sleep(0.5)                      # let the holder release the line
        except Exception:
            pass

    def send(self, data: str) -> None:
        self._open()
        self._t.send(data.encode())

    def _pump(self, timeout: float) -> None:
        chunk = self._t.recv(timeout)
        if chunk:
            self._buf += chunk.decode("utf-8", "replace")

    def expect(self, pattern: str, timeout: float = 12) -> str:
        _i, out = self.expect_any([pattern], timeout)
        return out

    def expect_any(self, patterns: list[str], timeout: float = 12):
        self._open()
        rxs = [re.compile(p, re.M) for p in patterns]
        end = time.time() + timeout
        while time.time() < end:
            for i, rx in enumerate(rxs):
                m = rx.search(self._buf)
                if m:
                    out = self._buf[:m.end()]
                    self._buf = self._buf[m.end():]
                    return i, out
            self._pump(0.3)
        raise TimeoutError(f"expect {patterns!r} timed out; tail={self._buf[-160:]!r}")

    def login(self, user: str, password: str, timeout: float = 30) -> bool:
        self.send("\r")
        i, _ = self.expect_any([r"login:\s*", _PROMPT], timeout)
        if i == 1:
            return True                       # already at a shell / CLI prompt
        self.send(user + "\r")
        self.expect(r"[Pp]assword:\s*", timeout)
        self.send(password + "\r")
        i, _ = self.expect_any([_PROMPT, r"login:\s*", r"[Ii]ncorrect"], timeout)
        return i == 0

    def sh(self, cmd: str, timeout: float = 20) -> CmdResult:
        self._buf = ""
        self.send(cmd + "\r")
        out = self.expect(_PROMPT, timeout)
        lines = out.splitlines()
        body = "\n".join(lines[1:-1]) if len(lines) >= 2 else out
        return CmdResult(0, body.strip())

    def close(self) -> None:
        if self._t is not None:
            try:
                self._t.send(b"\rexit\r")     # best-effort logout
                time.sleep(0.3)
            except Exception:
                pass
            try:
                self._t.close()
            finally:
                self._t = None
        self.ac.close()
