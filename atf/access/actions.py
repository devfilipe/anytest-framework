"""Node actions — named, bench-configured side-effect capabilities a test can invoke
(`power-cycle`, and later: provision-mgmt, trigger-update, factory-reset). An action models a
*physical/infra capability* (what the bench operator or an attacker can do), NOT a measurement
command on the board — so it stays consistent with the black-box rule.

Configured per board in the inventory (the bench maps each action's signals to commands on an
agent):

    actions:
      power-cycle:
        agent: power-node
        signals: { off: 'curl -s ".../Power1%20off"', on: 'curl -s ".../Power1%20on"',
                   status: 'curl -s ".../Power1"' }

The signal command runs on its agent (SSH). `ctx.actions` is the SDK surface:
`available()`, `has(name[, signal])`, `run(name, signal)`, and the `power_cycle(signal)`
convenience for the conventional `power-cycle` action.
"""
from __future__ import annotations

import time

from atf.access.agent import AgentConn

POWER = "power-cycle"


class ActionError(RuntimeError):
    pass


def _norm_signals(signals: dict) -> dict:
    """YAML 1.1 coerces bare `on`/`off` keys to booleans (the "Norway problem"); map them
    back to strings so an action works whether or not the bench quoted the keys."""
    out = {}
    for k, v in (signals or {}).items():
        key = "on" if k is True else "off" if k is False else str(k)
        out[key] = v
    return out


class Actions:
    def __init__(self, board_actions: dict, agents: dict):
        self._a = {name: {**spec, "signals": _norm_signals(spec.get("signals") or spec.get("actions"))}
                   for name, spec in (board_actions or {}).items()}
        self._agents = agents or {}
        self._conns: dict[str, AgentConn] = {}

    def available(self) -> list[str]:
        """Action names the bench configured for this board (each with an agent + ≥1 signal)."""
        return [n for n, s in self._a.items() if s.get("agent") and (s.get("signals"))]

    def has(self, name: str, signal: str | None = None) -> bool:
        spec = self._a.get(name)
        if not spec:
            return False
        return signal is None or signal in (spec.get("signals") or {})

    def _conn(self, agent_name: str) -> AgentConn:
        if agent_name not in self._agents:
            raise ActionError(f"action agent not in bench: {agent_name!r}")
        if agent_name not in self._conns:
            self._conns[agent_name] = AgentConn(self._agents[agent_name])
        return self._conns[agent_name]

    def run(self, name: str, signal: str, timeout: int = 30):
        """Run one action signal on its agent; returns the agent CmdResult."""
        spec = self._a.get(name)
        if not spec:
            raise ActionError(f"no such action: {name!r}")
        cmd = (spec.get("signals") or {}).get(signal)
        if not cmd:
            raise ActionError(f"action {name!r} has no signal {signal!r}")
        return self._conn(spec["agent"]).run(cmd, timeout=timeout)

    # --- power-cycle convenience (action named "power-cycle" with on/off/status signals) ---
    def power_cycle(self, signal: str):
        """Drive one power signal: "off" | "on" | "status"."""
        return self.run(POWER, signal)

    def cold_boot(self, off_secs: float = 5.0):
        """Cut power, hold, restore — a real cold boot (what a physical attacker does)."""
        self.power_cycle("off")
        time.sleep(off_secs)
        return self.power_cycle("on")

    def close(self):
        for c in self._conns.values():
            try:
                c.close()
            except Exception:
                pass
        self._conns.clear()
