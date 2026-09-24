"""Scaffold a new auto test: write `atf_checks/<model>/<slug>.py` from a template that exposes the
atf SDK (ctx drivers + actions). Backs `atf new-check` / the web scaffold — authoring a test is
"fill the TODOs". The file is flat under its model (the driver it uses is declared in `@register`,
not encoded in the path). No central import list: `atf_checks` discovers modules by walking the
package. (Manual tests are Markdown `.md` artifacts, scaffolded separately.)"""
from __future__ import annotations

import re
from pathlib import Path

_DRIVERS = ("host", "console", "craft", "mgmt")

_TEMPLATE = '''"""{title}

TODO: describe what this test verifies.

Verification must be BLACK-BOX: probe the board from a driver vantage (scan / TLS / reachability)
or drive a node action. Do NOT execute commands on the board — root/SSH access is a
development-only crutch that will be removed; a test that depends on it won't hold.

The Suite maps this test to requirement(s) — the test itself declares only the framework
capabilities it needs (drivers + actions).
"""
from __future__ import annotations

from atf.core.model import Ctx, Result, Severity, Verdict
from atf.core.registry import register


@register(id="{id}", drivers={drivers!r}, actions={actions!r},
          severity=Severity.{sev}, title="{title}")
def {fn}(ctx: Ctx) -> Result:
    # --- atf SDK (ctx) — the framework hands you exactly what you declared above ---
    #   drivers — comm channels the BENCH wires to an alias you declare; reach the target THROUGH
    #   the driver (ctx.<alias>.ip is its address — there is no static board IP):
    #     ctx.host                 local vantage: ctx.host.ping(ip), ctx.host.tcp(ip, port)
    #     ctx.mgmt.ip · .scan()    ip driver — network/mgmt vantage (nmap / tls / tcp)  drivers=("mgmt",)
    #     ctx.console.send/expect  serial driver — console channel                     drivers=("console",)
    #     ctx.craft.ip · .scan()   ip driver reached via an agent (craft / GL vantage)  drivers=("craft",)
    #   (the channel TYPE — serial vs ip — is set by the bench's driver instance, not by the check;
    #    the alias is just the ctx key. A board lacking your alias → the test is SKIPPED, not failed.)
    #   actions (node actions, configured by the bench):
    #     ctx.actions.power_cycle("off" | "on" | "status")                    actions=("power-cycle",)
    #   evidence: ctx.write_evidence(text) -> path (auto-named <check-id>-<board>.txt)
    {hint}
    # Optional setup / teardown: put teardown in `finally` so it ALWAYS runs (even on failure) —
    # e.g. restore the board with ctx.actions.power_cycle("on"). Remove the try/finally if unused.
    try:
        # --- setup (optional): prepare state / bring the board to a known point ---
        # --- test: the black-box probe / adversarial attempt ---
        # TODO: implement.
        ev = ctx.write_evidence("TODO: raw evidence")
        return Result(Verdict.MANUAL, title="TODO: one-line finding",
                      detail="not implemented yet", evidence=ev)
    finally:
        # --- teardown (optional): restore the board — runs even if the test raised ---
        pass
'''


def _slug(s: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower()


def render_check(*, id: str, drivers: list[str] | None = None, actions: list[str] | None = None,
                 severity: str = "medium", title: str = "") -> tuple[str, str, str]:
    """Render an auto-test `.py` from the template. Returns (slug, folder_dir, source_text) where
    folder_dir is the first driver (or "host"). Raises on a bad id/severity. Used both for a local
    write (`new_check`) and for scaffolding onto an agent's filesystem (server sends the text)."""
    drivers = [d for d in (drivers or []) if d and d != "host"]
    actions = actions or []
    title = title or id
    slug = _slug(id)
    if not slug:
        raise ValueError(f"invalid check id: {id!r}")
    from atf.core.model import Severity
    sev = severity.upper()
    if sev not in Severity.__members__:
        raise ValueError(f"bad severity {severity!r}; pick one of {[s.value for s in Severity]}")
    ddir = drivers[0] if drivers else "host"           # folder = primary driver
    if not drivers:
        hint = "# host-only black-box: ctx.host (ping/tcp) — no board login"
    else:
        hint = (f"# black-box from ctx.{drivers[0]}: e.g. ip = ctx.{drivers[0]}.ip; "
                f"ctx.{drivers[0]}.scan() — do NOT run commands on the board")
    text = _TEMPLATE.format(title=title, id=id, drivers=tuple(drivers), actions=tuple(actions),
                            sev=sev, fn=slug, hint=hint)
    return slug, ddir, text


def new_check(*, id: str, driver: str = "host", actions: list[str] | None = None,
              severity: str = "medium", title: str = "", model: str = "common",
              checks_root: str = "atf_checks") -> Path:
    """Create the auto-test module at `atf_checks/<model>/<slug>.py` locally (flat under the model —
    the driver is declared in `@register`, not the path). Returns the new file path. Raises on a bad
    driver/severity/model or an existing file."""
    slug, _ddir, text = render_check(id=id, drivers=[driver], actions=actions, severity=severity, title=title)
    mdl = _slug(model) if model else "common"
    root = Path(checks_root)
    pkg = root / mdl
    pkg.mkdir(parents=True, exist_ok=True)
    for p in (root, pkg):                           # ensure every level is a package
        (p / "__init__.py").touch(exist_ok=True)
    dest = pkg / f"{slug}.py"
    if dest.exists():
        raise FileExistsError(f"check module already exists: {dest}")
    dest.write_text(text)
    return dest
