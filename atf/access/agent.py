"""AgentConn — SSH connection to an agent node + platform-aware operations.

This is what decouples the AGENT (a node with a platform) from the VECTOR: the console
and craft channels are vector logic; the OS-specific bits (ping, tcp scan, serial bridge)
live here and dispatch on `agent.platform` (linux | windows). So any agent can serve any
vector — the console/craft binding chooses which agent bridges it, whatever machine that is.
"""
from __future__ import annotations

import shlex

from atf.access.channels.base import CmdResult

# Inline serial bridge for linux agents without socat: copy stdin→tty and tty→stdout,
# unbuffered, until stdin closes (the SSH session ends) or the tty goes away.
_PY_BRIDGE = """\
import os, select, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY)
i, o = sys.stdin.fileno(), sys.stdout.fileno()
def put(f, b):
    while b:
        b = b[os.write(f, b):]
while True:
    r = select.select([i, fd], [], [])[0]
    if i in r:
        b = os.read(i, 4096)
        if not b:
            break
        put(fd, b)
    if fd in r:
        try:
            b = os.read(fd, 4096)
        except OSError:
            break
        if not b:
            break
        put(o, b)
"""


def serial_bridge_cmd(dev: str, baud: int, bridge: str) -> str:
    """Shell command that bridges serial `dev` to the command's stdin/stdout on a linux agent."""
    if bridge == "socat":
        return f"socat - {dev},b{baud},raw,echo=0"
    q = shlex.quote(dev)
    return (f"stty -F {q} {int(baud)} raw -echo && "
            f"exec python3 -u -c {shlex.quote(_PY_BRIDGE)} {q}")


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

    def failure(self) -> str:
        """Why the remote bridge is gone ('exit N: <stderr>'), or '' while it still runs.
        A bridge that dies at start (tty permission, missing device, no tool) would otherwise
        only show as an expect timeout with nothing read."""
        if self._sock:
            return ""
        try:
            if not self._o.exit_status_ready():
                return ""
            err = b""
            while self._o.recv_stderr_ready():
                chunk = self._o.recv_stderr(4096)
                if not chunk:
                    break
                err += chunk
            rc = self._o.recv_exit_status()
        except Exception:
            return ""
        return f"exit {rc}: {err.decode('utf-8', 'replace').strip()}"

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
        self._bridge = None

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

    def serial_bridge(self) -> str:
        """The serial bridge this linux agent has: 'socat', else 'python3'. Probed once."""
        if self._bridge is None:
            for tool in ("socat", "python3"):
                if self.run(f"command -v {tool}").rc == 0:
                    self._bridge = tool
                    break
            else:
                raise RuntimeError(
                    f"agent {self.agent.name!r}: neither socat nor python3 is available "
                    "to bridge the serial console; install one of them on the agent")
        return self._bridge

    def serial_stream(self, dev: str, baud: int) -> _StreamT:
        """Bridge a serial device over SSH; returns a raw send/recv transport."""
        if self.platform == "windows":
            # best-effort (untested): plink (PuTTY) bridging a COM port
            cmd = f"plink -serial {dev} -sercfg {baud},8,n,1,N"
        else:
            cmd = serial_bridge_cmd(dev, baud, self.serial_bridge())
        chan = self._c().get_transport().open_session()
        chan.exec_command(cmd)
        return _StreamT(chan, is_socket=False)

    def close(self) -> None:
        if self._ssh is not None:
            try:
                self._ssh.close()
            finally:
                self._ssh = None
