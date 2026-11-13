"""Comm channels chosen by driver type. Guards the craft/mgmt → IpChannel unification contract."""
from __future__ import annotations

import socket


def test_ip_channel_exposes_ip_not_board_ip():
    from atf.access.channels.ip import IpChannel
    ch = IpChannel({"type": "ip", "ip": "127.0.0.1"}, None, {})
    assert ch.ip == "127.0.0.1"
    assert ch.type == "ip"
    assert not hasattr(ch, "board_ip")     # the old CraftChannel attr is gone (craft-reachability regression)


def test_ip_channel_tcp_probe_open_and_closed():
    from atf.access.channels.ip import IpChannel
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    ch = IpChannel({"ip": "127.0.0.1"}, None, {})
    assert ch.tcp("127.0.0.1", port) is True       # listening
    srv.close()
    assert ch.tcp("127.0.0.1", port) is False      # closed


def test_ip_channel_ping_returns_bool():
    from atf.access.channels.ip import IpChannel
    assert isinstance(IpChannel({"ip": "127.0.0.1"}, None, {}).ping(), bool)


def test_ping_raises_when_the_vantage_has_no_probe_tool(monkeypatch):
    """A missing ping/nmap must NOT read as 'board down' — that is what reported live boards as
    unreachable from the atf-mgmt container, which shipped without iputils-ping."""
    import pytest

    from atf.access import host as host_mod
    from atf.access.channels.ip import IpChannel
    monkeypatch.setattr(host_mod.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="no ICMP probe"):
        IpChannel({"ip": "127.0.0.1"}, None, {}).ping()
    with pytest.raises(RuntimeError, match="no ICMP probe"):
        host_mod.HostProbe().ping("127.0.0.1")


def test_ping_falls_back_to_nmap_when_only_nmap_is_present(monkeypatch):
    import subprocess

    from atf.access import host as host_mod
    monkeypatch.setattr(host_mod.shutil, "which",
                        lambda name: "/usr/bin/nmap" if name == "nmap" else None)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="Host is up (0.1s latency).\n", stderr="")

    monkeypatch.setattr(host_mod.subprocess, "run", fake_run)
    assert host_mod.icmp_ping("127.0.0.1") is True
    assert calls and calls[0][0] == "nmap" and "-sn" in calls[0]


def test_serial_channel_type():
    from atf.access.channels.console import SerialChannel
    ch = SerialChannel(None, {"transport": "ssh", "device": "/dev/ttyUSB0", "baud": 115200})
    assert ch.type == "serial"


# --- serial bridge on a linux agent (socat, else an inline python3 bridge) ---

class _FakeRunAgentConn:
    """An AgentConn whose `run` answers `command -v <tool>` from a set of installed tools."""

    def __new__(cls, installed, name="pi-agent"):
        from atf.access.agent import AgentConn
        from atf.access.channels.base import CmdResult
        from atf.core.inventory import Agent
        ac = AgentConn(Agent(name=name, platform="linux", host="192.0.2.1"))
        ac.calls = []

        def run(cmd, timeout=60):
            ac.calls.append(cmd)
            for tool in ("socat", "python3"):
                if cmd == f"command -v {tool}":
                    found = tool in installed
                    return CmdResult(0 if found else 1, f"/usr/bin/{tool}\n" if found else "")
            return CmdResult(127, "", "unexpected")
        ac.run = run
        return ac


def test_serial_bridge_prefers_socat_and_caches_the_choice():
    ac = _FakeRunAgentConn({"socat", "python3"})
    assert ac.serial_bridge() == "socat"
    assert ac.serial_bridge() == "socat"
    assert ac.calls == ["command -v socat"]          # probed once per AgentConn


def test_serial_bridge_falls_back_to_python3_without_socat():
    ac = _FakeRunAgentConn({"python3"})
    assert ac.serial_bridge() == "python3"
    assert ac.serial_bridge() == "python3"
    assert ac.calls == ["command -v socat", "command -v python3"]


def test_serial_bridge_without_socat_or_python3_names_the_agent():
    import pytest
    ac = _FakeRunAgentConn(set(), name="pi-agent")
    with pytest.raises(RuntimeError, match=r"pi-agent.*neither socat nor python3"):
        ac.serial_bridge()


def test_serial_bridge_cmd_socat_is_unchanged():
    from atf.access.agent import serial_bridge_cmd
    assert serial_bridge_cmd("/dev/ttyUSB0", 115200, "socat") == \
        "socat - /dev/ttyUSB0,b115200,raw,echo=0"


def test_python3_bridge_moves_bytes_both_ways_over_a_pty():
    """Run the exact shell command the agent would run, locally, against a pty pair."""
    import os
    import select
    import subprocess
    import time

    from atf.access.agent import serial_bridge_cmd
    master, slave = os.openpty()
    dev = os.ttyname(slave)
    proc = subprocess.Popen(["sh", "-c", serial_bridge_cmd(dev, 115200, "python3")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        def read_until(fd, want, timeout=5.0):
            got = b""
            end = time.time() + timeout
            while want not in got and time.time() < end:
                r, _, _ = select.select([fd], [], [], 0.2)
                if r:
                    got += os.read(fd, 4096)
            return got

        proc.stdin.write(b"to-board\r")
        proc.stdin.flush()
        assert b"to-board\r" in read_until(master, b"to-board\r")      # stdin → tty, raw (no \r→\n)
        os.write(master, b"from-board\n")
        assert b"from-board\n" in read_until(proc.stdout.fileno(), b"from-board\n")  # tty → stdout
        proc.stdin.close()                                              # stdin closes → bridge exits
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
        os.close(slave)


def test_python3_bridge_reports_a_missing_device_on_stderr():
    import subprocess

    from atf.access.agent import serial_bridge_cmd
    p = subprocess.run(["sh", "-c", serial_bridge_cmd("/dev/atf-no-such-tty", 115200, "python3")],
                       input=b"", capture_output=True, timeout=10)
    assert p.returncode != 0
    assert b"atf-no-such-tty" in p.stderr


class _DeadChan:
    """A paramiko-like session channel whose remote command already exited with an error."""

    def __init__(self, rc, err):
        self._rc, self._err = rc, err

    def settimeout(self, _t):
        pass

    def recv(self, _n):
        return b""                                   # EOF: the bridge is gone

    def sendall(self, _b):
        pass

    def exit_status_ready(self):
        return True

    def recv_exit_status(self):
        return self._rc

    def recv_stderr_ready(self):
        return bool(self._err)

    def recv_stderr(self, n):
        out, self._err = self._err[:n], self._err[n:]
        return out

    def close(self):
        pass


def test_serial_timeout_surfaces_a_failed_bridge_stderr():
    import pytest

    from atf.access.agent import _StreamT
    from atf.access.channels.console import SerialChannel

    class AC:
        platform = "linux"

        def run(self, cmd, timeout=60):
            pass

        def serial_stream(self, dev, baud):
            return _StreamT(_DeadChan(1, b"open /dev/ttyUSB0: Permission denied\n"), is_socket=False)

        def close(self):
            pass

    ch = SerialChannel(AC(), {"transport": "ssh", "device": "/dev/ttyUSB0", "baud": 115200})
    with pytest.raises(TimeoutError, match=r"exit 1.*Permission denied"):
        ch.expect("login:", timeout=0.5)
