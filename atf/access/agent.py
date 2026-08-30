"""AgentConn — SSH connection to an agent node + platform-aware operations.

This is what decouples the AGENT (a node with a platform) from the VECTOR: the console
and craft channels are vector logic; the OS-specific bits (ping, tcp scan, serial bridge)
live here and dispatch on `agent.platform` (linux | windows). So any agent can serve any
vector — the console/craft binding chooses which agent bridges it, whatever machine that is.
"""
from __future__ import annotations

import shlex

from atf.access.channels.base import CmdResult


class _StreamT:
    """Raw byte transport over a paramiko channel or a socket (for send/expect)."""
    def __init__(self, obj, is_socket: bool):
        self._o = obj
        self._sock = is_socket

    def send(self, b: bytes) -> None:
        (self._o.sendall if not self._sock else self._o.sendall)(b)

    def recv(self, timeout: float) -> bytes:
        self._o.settimeout(timeout)
        try:
            return self._o.recv(4096)
        except Exception:
            return b""

    def close(self) -> None:
        try:
            self._o.close()
        except Exception:
            pass


class AgentConn:
    def __init__(self, agent):
        self.agent = agent
        self.platform = (agent.platform or "linux").lower()
        self._ssh = None

    def _c(self):
        if self._ssh is None:
            import paramiko
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            pw = self.agent.ssh_password or None
            key = getattr(self.agent, "ssh_key", "") or None
            # password when one is configured; otherwise fall back to key auth (a configured key
            # file, the ssh-agent, or the user's default ~/.ssh keys) so passwordless hosts work
            c.connect(self.agent.host, port=int(getattr(self.agent, "ssh_port", 22) or 22),
                      username=self.agent.ssh_user,
                      password=pw, key_filename=key, timeout=15,
                      allow_agent=(pw is None), look_for_keys=(pw is None))
            self._ssh = c
        return self._ssh

    def run(self, cmd: str, timeout: int = 60) -> CmdResult:
        _in, out, err = self._c().exec_command(cmd, timeout=timeout)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        return CmdResult(out.channel.recv_exit_status(), o, e)

    # --- platform-aware ops ---
    def ping(self, target: str) -> bool:
        if self.platform == "windows":
            return "TTL=" in self.run(f"ping -n 1 -w 1500 {target}").out.upper()
        return self.run(f"ping -c 1 -W 2 {target}").rc == 0

    def tcp_scan(self, target: str, ports) -> list[int]:
        if self.platform == "windows":
            plist = ",".join(str(p) for p in ports)
            ps = (
                f"foreach($p in {plist})"
                + "{$c=New-Object Net.Sockets.TcpClient;"
                + f"$r=$c.BeginConnect('{target}',$p,$null,$null);"
                + "if($r.AsyncWaitHandle.WaitOne(800) -and $c.Connected){('{0} open' -f $p)};$c.Close()}"
            )
            out = self.run(f'powershell -NoProfile -Command "{ps}"').out
        else:  # linux: dep-free bash /dev/tcp connect, bounded by `timeout`
            script = "; ".join(
                f'timeout 1 bash -c "echo >/dev/tcp/{target}/{p}" 2>/dev/null && echo "{p} open"'
                for p in ports)
            out = self.run(f"bash -c {shlex.quote(script)}").out
        return sorted(int(line.split()[0]) for line in out.splitlines()
                      if line.strip().endswith("open"))

    def serial_stream(self, dev: str, baud: int) -> _StreamT:
        """Bridge a serial device over SSH; returns a raw send/recv transport."""
        if self.platform == "windows":
            # best-effort (untested): plink (PuTTY) bridging a COM port
            cmd = f"plink -serial {dev} -sercfg {baud},8,n,1,N"
        else:
            cmd = f"socat - {dev},b{baud},raw,echo=0"
        chan = self._c().get_transport().open_session()
        chan.exec_command(cmd)
        return _StreamT(chan, is_socket=False)

    def close(self) -> None:
        if self._ssh is not None:
            try:
                self._ssh.close()
            finally:
                self._ssh = None
