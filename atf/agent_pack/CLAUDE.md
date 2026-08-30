# Authoring & running atf tests

You are helping a developer/tester **use the atf test framework** — authoring tests, mapping
them to requirements in suites, and running them against a bench. This file (and the skills next
to it) were installed by the atf agent when the user turned on AI. It is about **using** atf, not
developing the framework itself.

## The mental model

- A **test** lives as a file in a check-source repo the agent serves, under
  `atf_checks/<model>/<driver>/<id>.py` (automated) or `atf_checks/<model>/manual/<id>.md` (manual).
  - `<model>` = `common` (any board) or a model slug (e.g. `router-x`) — runs only on that model.
- A test **declares the framework capabilities it needs** — nothing more:
  - **drivers** (comm channels): an **alias** the bench wires to a driver instance. `console`,
    `craft`, `mgmt` are the conventional aliases (plus the implicit `host`). The driver's **type**
    (`serial` or `ip`) — and thus which methods `ctx.<alias>` exposes — is set by the **bench**, not
    the test; the alias is just the ctx key.
  - **actions** (node actions): system-defined, e.g. `power-cycle`.
  The **bench** provides them; the runner **skips** a test whose driver/action the bench lacks.
- A test does **not** own its requirements. The **Suite** maps requirement ↔ test (many-to-many,
  ordered). `requirements=[...]` on a test is only an *advisory suggestion* for the suite editor.
- A **Suite** is the requirement→test map (ordered — run order = the order you see; put setup
  first, teardown last). A **Test Plan** = Suite + bench/board.

## The SDK — `ctx` (what a test receives)

Reach the target **through** the driver — `ctx.<alias>.ip` is its address (there is no static board
IP). Methods depend on the driver **type** the bench wired to that alias:

```python
def my_test(ctx) -> Result:
    ctx.host                      # local vantage: ctx.host.ping(ip), ctx.host.tcp(ip, port)
    ctx.mgmt.ip                    # ip driver — target address; also .ping()/.tcp()/.scan()/.nse()  drivers=("mgmt",)
    ctx.mgmt.scan()                # e.g. nmap from the mgmt/network vantage (defaults to ctx.mgmt.ip)
    ctx.console.send(...) ; ctx.console.expect(...)   # serial driver: send/expect/login/sh   drivers=("console",)
    ctx.craft.ip                   # ip driver reached via an agent (craft / GL vantage)          drivers=("craft",)
    ctx.actions.power_cycle("off"|"on"|"status")   # node action                               actions=("power-cycle",)
    ev = ctx.write_evidence(text) # persists evidence, auto-named <check-id>-<board>.txt
    return Result(Verdict.GAP|PASS|MANUAL|ERROR, title="…", detail="…", evidence=ev)
```

A board that doesn't wire your alias → the test is **SKIPPED** (not failed).

## THE BLACK-BOX RULE (non-negotiable)

Verification must be **black-box**: probe the board from a driver vantage (scan / TLS / reachability),
inspect from outside, or drive a node action. **Do NOT run commands on the board** to measure it —
root/SSH is a dev-only crutch that will be removed; a test that depends on it does not hold.
- **Sanctioned exception**: an adversarial attempt — "try to get root / bypass auth; success ⇒ GAP,
  rejected ⇒ PASS" — is legitimate and self-correcting (e.g. the GRUB `init=/bin/sh` test).

## Automated test skeleton (`.py`)

```python
from atf.core.model import Ctx, Result, Severity, Verdict
from atf.core.registry import register

@register(id="mgmt-tls-enum", drivers=("mgmt",), actions=(), severity=Severity.MEDIUM,
          title="Legacy TLS enumerated")
def mgmt_tls_enum(ctx: Ctx) -> Result:
    # black-box from ctx.mgmt — never run commands on the board
    ...
    return Result(Verdict.GAP, title="…", detail="…", evidence=ctx.write_evidence("…"))
```

## Manual test artifact (`.md`)

Frontmatter + Markdown steps the operator runs. Drivers/actions are usually empty (a manual test
is operator-driven, always available); declare them only if the run must gate on a channel.

```markdown
---
id: manual-uart-photo
title: Debug interface photo
severity: high
drivers: []
actions: []
---
## Objetivo
…
## Passos
1. …
## Veredito
- **pass** se …
- **gap** se …
```

## Workflow (prefer the skills)

1. **Scaffold** a test (creates the file on your repo, with the SDK): use the `atf-author-driver-test`
   or `atf-author-manual-test` skill, or the web UI (Studio › Tests › New).
2. **Implement** the black-box logic / write the operator steps.
3. **Map** it to a requirement in a Suite (Studio › Suites) — order matters (setup→…→teardown).
4. **Run** a Suite / ad-hoc and read the report: use the `atf-run` skill.

## Reaching the framework

When AI was turned on, the agent wrote `.atf-ai.env` here with:
`ATF_SERVER` (the atf server), `ATF_TOKEN` (your session token), `ATF_AID` (this agent's id),
`ATF_SOURCES` (the local check-source repo paths — **this is where the test files live; edit them
directly**).

**Prefer the `atf` MCP tools** (auto-configured via `.mcp.json`) over raw curl:
- `atf_catalog` — list all known tests (id, drivers, actions, mode, model).
- `atf_suites` — list saved suites (requirement→test maps).
- `atf_scaffold` — scaffold a test on the agent's repo (`kind: auto|manual`, drivers/actions).
- `atf_run` — run a suite or ad-hoc ids against a bench/board.
- `atf_report` — fetch a run's report (verdicts, drivers/actions, skip reasons, roll-up).

Authoring/editing files is done directly in the repos under `ATF_SOURCES` (Claude Code edits them).
Never hardcode credentials; the MCP server reads them from `.atf-ai.env` / the environment. If the
server was restarted, `ATF_TOKEN` may have expired — turn AI off/on again to refresh it.
