"""Channel base — uniform transport to a board through one driver.

Concrete channels are chosen by the driver's TYPE:
  - SerialChannel (type "serial"): console over ssh+serial / ser2net — send/expect/login/sh
  - IpChannel     (type "ip"):     management/craft over the network — ping/tcp/scan/nse/sh
                                   (with an agent = probe from the agent's vantage; without = host/container)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class CmdResult:
    rc: int
    out: str
    err: str = ""


class Channel:
    """A comm driver instance in the run context (``ctx.<alias>``). Subclasses set ``type`` and
    expose the props/methods their checks use."""
    type: str = ""

    def sh(self, cmd: str, timeout: int = 30) -> CmdResult:
        """Run a shell command ON THE BOARD via this driver's vantage (if supported)."""
        raise NotImplementedError

    def close(self) -> None:  # optional
        pass
