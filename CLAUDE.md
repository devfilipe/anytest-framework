# CLAUDE.md — Anytest Framework engine

Guide for [Claude Code](https://claude.com/claude-code) when **developing the framework itself**.
This repo is the **engine**: the runner, access channels, config store, web API/UI and CLI.
Check code, benches, suites and requirement catalogs live in *separate* check-source repos and
are discovered at runtime — they are not here (except the tiny `examples/` set).

## Architecture

```
atf/
  cli.py                      entry point: run · list · suites · report · new-check · web · agent
  core/                       transport-agnostic spine (knows no channel)
    model.py                  Ctx, Result, Verdict, Severity, CheckSpec dataclasses
    registry.py               @register + select()/resolve_selection() (suite → CheckSpecs)
    checks.py                 discovery: import atf_checks.* from $ATF_CHECK_SOURCES
    inventory.py              bench YAML/DB → typed Bench/Board/Agent
    runner.py                 execute checks, gate on available drivers/actions, batch mgmt
    report.py                 records → matrix + findings + consolidated report.md
    scaffold.py               new-check code/markdown templates
    manual.py                 operator-driven manual checks
  access/                     how a board is reached
    host.py                   the host vantage (ping/tcp/http)
    agent.py                  dev/host agent: stdlib, outbound long-poll to the server
    actions.py               node actions (power-cycle) — Actions/ctx.actions
    channels/{base,console(SerialChannel),ip}.py   comm channels by driver TYPE: serial | ip
    mgmt/{dispatch,worker}.py  host dispatcher (docker run) + in-container worker (ip-without-agent)
  store/                      SQLite config store (source of truth)
    schema.sql · db.py · crypto.py (Fernet, keyed by APP_SECRET) · repo.py (all queries)
  web/
    server.py                 FastAPI app: auth gate, REST API, pilot runs, agent hub
    agents.py · locks.py      agent transport + per-resource run locks
    static/index.html         the ENTIRE SPA (vanilla JS, no build step)
  agent_pack/                 the resource pack shipped to a connected agent's Claude (MCP + skills)
docker/atf-mgmt.Dockerfile     the mgmt toolbox image
examples/                     a runnable example check-source (host-recon + a bench)
```

## Core principles (do not break)

- **Any test — black-box by default for security.** A check is just `fn(ctx) -> Result`; it can
  verify anything (security, compliance, functional). *Security* checks default to **black-box**:
  probe from outside (network/console/craft/host) or ask an operator, don't log into the device to
  read config as a measurement shortcut (the one sanctioned device interaction is the adversarial
  *"try root; success ⇒ finding, rejected ⇒ pass"* pattern). Don't hard-wire that assumption into
  the engine — it's a check-authoring convention, not an engine constraint.
- **Checks declare capabilities, not requirements.** `@register(id, drivers=(), actions=(), …)`.
  A test never lists requirements — the **suite** owns the requirement→test map. Don't reintroduce
  a `requirements=` argument.
- **Drivers & actions are user-constructible Inventory entities.** A driver has a built-in TYPE
  (`serial` = console; `ip` = network target) + an **alias** (the ctx key). A board (in a bench)
  instantiates a driver and parameterizes it (agent, ip, device, baud); creds are bench-scoped too.
  A check reaches a driver as `ctx.<alias>` and the target address as `ctx.<alias>.ip` — the board
  has **no** static mgmt ip. Actions are entities too (`name` + signals; built-in `power-cycle`).
  The implicit `host` driver (`ctx.host`) is always present. The runner **skips** a check whose
  driver-alias/action the bench lacks — never hard-fail for a missing capability.
- **Config store is the source of truth**, YAML only seeds/imports/exports it. Secrets are always
  encrypted via `APP_SECRET` — keep it stable, never log or export plaintext.
- **Vendor-agnostic.** No product/vendor names anywhere in the engine. Keep examples generic
  (`router-x`, `acme:E.3`, `board-1`).

## Run it in dev

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[host,web]"
export APP_SECRET=dev-secret ATF_CHECK_SOURCES="$PWD/examples"
.venv/bin/atf list                     # host-recon (from ./examples)
.venv/bin/atf run --id host-recon --bench examples/benches/lab.yaml --mgmt-backend local
.venv/bin/atf web                       # http://127.0.0.1:8899  ·  API docs /api/docs
```

`ATF_CHECK_SOURCES` is `os.pathsep`-separated repo roots (each containing `atf_checks/`). With
nothing configured there are no checks. The mgmt toolbox image is built with `make image`.

> ⚠️ **The `atf-mgmt` image bakes the framework code.** After changing anything under `atf/`, rebuild
> it with `make image` before running `mgmt` (ip-without-agent) checks on the `docker` backend —
> otherwise dispatch runs stale code (or fails if the image is missing). Or use `--mgmt-backend local`
> (needs `nmap` on the host) to skip the container in dev.

## Conventions

- **Python**: Ruff, line length 120, double quotes, spaces. `make check` = compile + import
  sanity + ruff. Run it before finishing a change.
- **The SPA** (`web/static/index.html`) is a single self-contained file: vanilla JS, inline CSS,
  no bundler, no external requests. Keep it that way; it renders Markdown with the in-file `md()`.
- **The store**: every schema change touches `store/schema.sql` **and** `store/repo.py`. The DB is
  created/migrated on first `atf web`.
- **Auth**: all `/api/*` is behind a login gate except an explicit allow-list (`server.py`
  `_login_gate`) — login, agent endpoints, the SSE stream, and the API docs.

## Skills

Task workflows live in `.claude/skills/`:

- **dev-server** — run the API + dashboard against the example check-source.
- **add-driver** — add a new access channel type (the `SerialChannel`/`IpChannel` pattern in `access/channels/`).
- **web-api** — work on the FastAPI backend + the single-file SPA safely.
- **store-schema** — evolve the SQLite config store (schema + repo + crypto).
