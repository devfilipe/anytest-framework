"""Example check: board reachability + open management ports, from the host vantage.

The simplest possible test — no channel, no container. It validates the whole loop
(inventory -> check -> result -> report) end to end and doubles as a template for your
own checks. A test declares only the framework capabilities it needs; `host-recon` needs
none (it probes from the host that runs the framework), so it is always available.

Drop your real checks in their own repo under `atf_checks/<model>/<id>.py` (flat under the model —
the driver a test uses is declared in `@register`, not the path) and point `ATF_CHECK_SOURCES` at
it. `common` = runs on any board; a `<model>` slug = only on boards of that model.
"""
from __future__ import annotations

from atf.core.model import Ctx, Result, Severity, Verdict
from atf.core.registry import register

MGMT_PORTS = [22, 80, 443, 830, 8080]


@register(id="host-recon", drivers=("mgmt",), actions=(),
          severity=Severity.INFO,
          title="Board reachability & open mgmt ports (host vantage)")
def host_recon(ctx: Ctx) -> Result:
    ip = ctx.mgmt.ip
    if not ip:
        return Result(Verdict.SKIPPED, detail="board has no management ip in the bench")

    up = ctx.host.ping(ip)
    open_ports = [p for p in MGMT_PORTS if ctx.host.tcp(ip, p)]
    ev = ctx.write_evidence(
        f"host recon of {ctx.board.name} ({ip})\n"
        f"ping: {'up' if up else 'down'}\n"
        f"open tcp mgmt ports: {open_ports}\n"
    )

    if not up and not open_ports:
        return Result(Verdict.ERROR, Severity.INFO,
                      title=f"{ip} unreachable from host",
                      detail="no ICMP reply and no open mgmt port", evidence=ev)

    return Result(Verdict.PASS, Severity.INFO,
                  title=f"{ctx.board.name} reachable ({ip})",
                  detail=f"ping={'up' if up else 'down'}; open={open_ports}",
                  evidence=ev, metrics={"ping": up, "open_tcp": open_ports})
