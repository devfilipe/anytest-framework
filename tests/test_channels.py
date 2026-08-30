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


def test_serial_channel_type():
    from atf.access.channels.console import SerialChannel
    ch = SerialChannel(None, {"transport": "ssh", "device": "/dev/ttyUSB0", "baud": 115200})
    assert ch.type == "serial"
