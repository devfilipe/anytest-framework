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
