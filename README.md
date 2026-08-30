# Anytest Framework (`atf`)

An **extensible test framework for network elements and embedded devices** — security,
compliance, and functional checks alike. You describe *what to test* (checks grouped into
suites) and *where* (a bench of boards and the agents that reach them); `atf` runs the checks,
collects evidence, and rolls the results up into a report and a requirement×board matrix. It
ships a CLI, a FastAPI backend with a single-page dashboard, a zero-install remote agent, and
an optional AI wizard.

> **Vendor-agnostic.** The engine knows nothing about any specific product. Devices, checks,
> requirement catalogs and benches all live *outside* the engine and are loaded at runtime.

## Why

- **Any kind of test.** Security probes, compliance checks, functional or operational
  verification — a check is just `fn(ctx) -> Result`. Security checks default to **black-box**
  (probe from outside, don't lean on device logins as a shortcut), but the framework doesn't
  force it.
- **Tests are files, decoupled from requirements.** A check declares only the *capabilities*
  it needs (`drivers` + `actions`). A **suite** owns the requirement→test mapping, so the same
  check can satisfy different compliance catalogs without edits.
- **Runs anywhere the bench can reach.** A driver (a serial or IP channel the check names by an
  alias like `console`/`mgmt`) is bridged to a board by an **agent** — any machine (Linux/Windows)
  that can physically reach it. The runner skips a check whose driver/action the bench doesn't provide.

## Install

```bash
git clone https://github.com/devfilipe/anytest-framework
cd anytest-framework
python3 -m venv .venv
.venv/bin/pip install -e ".[host,web]"      # host = console/craft channels; web = API + dashboard
```

## Quickstart

The repo ships a tiny example check-source so you can see the loop end to end with no device:

```bash
export APP_SECRET="a-long-random-secret"     # at-rest key for stored secrets (keep it stable)
export ATF_CHECK_SOURCES="$PWD/examples"     # where your checks/benches live

.venv/bin/atf list                           # → host-recon  (discovered from ./examples)
.venv/bin/atf run --id host-recon \
  --bench examples/benches/lab.yaml --mgmt-backend local
# → ran 1 check-result across 1 board · verdicts: {'pass': 1}

.venv/bin/atf web                            # dashboard + API on http://127.0.0.1:8899
```

Open <http://127.0.0.1:8899> (default login `admin` / `admin`, change it) and the API docs at
<http://127.0.0.1:8899/api/docs>.

For a fuller, runnable example, point `ATF_CHECK_SOURCES` at
**[anytest-checks-common](https://github.com/devfilipe/anytest-checks-common)** — model-agnostic
security checks (open ports, weak TLS, default credentials) plus a `docker-compose.yml` that spins
up a deliberately-vulnerable target so the checks produce real findings against your own machine:

```bash
git clone https://github.com/devfilipe/anytest-checks-common
cd anytest-checks-common && docker compose up -d          # unauthenticated Redis on :6379
ATF_CHECK_SOURCES="$PWD" atf run --suite baseline \
  --bench benches/localhost.yaml --mgmt-backend local     # → host-open-ports/mgmt-port-scan: GAP
docker compose down
```

To test your own device, point `ATF_CHECK_SOURCES` at one or more **check-source repos** of
your own (see [Concepts](#concepts)) and write a bench for it.

## Deploy on a server

For a shared/persistent install (not just a dev shell):

1. **Install** the engine:
   ```bash
   git clone https://github.com/devfilipe/anytest-framework && cd anytest-framework
   python3 -m venv .venv && .venv/bin/pip install -e ".[host,web]"
   ```
2. **Build the `atf-mgmt` image** — required for the default `mgmt` backend (the `ip`-driver checks
   run `nmap` etc. inside it). Needs Docker on the server:
   ```bash
   make image     # docker build -f docker/atf-mgmt.Dockerfile -t atf-mgmt:latest .
   ```
   > ⚠️ The image **bakes the framework code**. **Rebuild it (`make image`) every time you update
   > the framework**, or dispatched `mgmt` checks fail. (For a host without Docker, run with
   > `--mgmt-backend local` and install `nmap` on the host instead.)
3. **Configure** (persist these — e.g. an env file `source`d by your service manager):
   ```bash
   export APP_SECRET="<32+ random chars>"        # at-rest key for secrets — MUST stay stable
   export DATABASE_URL="file:/var/lib/atf/atf.db" # the config store (persistent path)
   export ATF_CHECK_SOURCES="/srv/anytest-checks-common:/srv/anytest-checks-router-x"  # your check repos
   # export PUBLIC_HOST="atf.example.com"         # only if the browser reaches the app from another host (CORS)
   ```
   Instead of `ATF_CHECK_SOURCES` you can register repos at runtime under **Admin › Repositories**
   (the server git-clones/syncs them; private repos take an encrypted token).
4. **Run** it (bind to a real address, behind a reverse proxy if public):
   ```bash
   .venv/bin/atf web --host 0.0.0.0 --port 8899
   ```
   Run it under a process manager (systemd, supervisor, docker) so it restarts on reboot. Example
   systemd `ExecStart`: `/srv/anytest-framework/.venv/bin/atf web --host 0.0.0.0 --port 8899` with the
   config above in the unit's `EnvironmentFile`.
5. **Secure it**: log in (`admin` / `admin`) and change the admin password immediately (Admin › Users).
   The API is behind a login gate; only `/api/docs` and the login endpoint are open.

Update flow later: `git pull` → `pip install -e '.[host,web]'` (if deps changed) → **`make image`** →
restart the service.

## Concepts

| Term | What it is |
|------|-----------|
| **Check** | A test: `@register`-decorated `fn(ctx) -> Result` in a `.py` file, or a Markdown manual procedure (`.md`). Declares `drivers` + `actions` it needs. |
| **Driver** | A comm channel the bench wires to a board, named by an **alias** the check declares (`console`, `craft`, `mgmt`, …). Built-in channel **types**: `serial` (console) and `ip` (network/craft/mgmt — carries the target address); the bench picks the type + values. Plus the implicit `host` (local vantage). |
| **Action** | A node action the bench can perform, e.g. `power-cycle`. System-defined; the bench wires *how*. |
| **Suite** | An ordered map of *requirements → the checks that satisfy them*. The unit you run. |
| **Bench** | The *where*: boards under test + the agents that bridge each driver to them. |
| **Agent** | A machine that reaches a board and bridges a driver. Runs the zero-install `atf agent`, connects out to the server. |
| **Requirement catalog** | A namespaced set of requirements (e.g. `acme:E.3`) with descriptions + how-to-verify text. |

A check-source repo contributes to the `atf_checks` namespace:

```
atf_checks/
  common/…/<id>.py         # automated check — runs on any board
  <model-slug>/…/<id>.py   # runs only on boards of that model
  <model-slug>/…/<id>.md   # a manual (operator-run) test
```

The engine keys only on the first segment (`common` or a `<model-slug>`); everything below it is
free-form organization — a check declares the drivers/actions it needs via `@register`, not the path.

[**anytest-checks-common**](https://github.com/devfilipe/anytest-checks-common) is a ready-made
example of such a repo (common checks + a vulnerable target). Add per-model repos
(`atf_checks/<model-slug>/…`) for device-specific checks.

## Architecture

```
                CLI  ·  FastAPI backend + SPA dashboard  ·  remote agents  ·  AI wizard
                                        │
     ┌──────────────────────────────────┴──────────────────────────────────┐
     │  atf/ (this repo, the engine)                                       │
     │    core/     model · registry · inventory · runner · report         │  transport-agnostic spine
     │    access/   host + channels/{console,ip} + mgmt dispatch           │  how a board is reached
     │    store/    SQLite config store (benches/suites/secrets, encrypted)│
     │    web/       FastAPI API + single-file SPA (static/index.html)     │
     └─────────────────────────────────────────────────────────────────────┘
                                        │  ATF_CHECK_SOURCES (runtime)
                    check-source repos (yours) ── atf_checks/… · benches/ · suites/ · requirements/
```

- **`core`** is the spine and knows no transport. **`access`** reaches boards. **`mgmt`** checks
  run inside a small Docker toolbox image (`atf-mgmt`, `nmap` etc.); check code is *mounted*, not
  baked, so editing a check needs no rebuild.
- The **config store** is SQLite and is the source of truth; YAML under your check repos seeds
  it on first run. Secrets are encrypted at rest with `APP_SECRET`. The CLI resolves store entities
  too: `atf run --bench "<name>" --suite "<name>"` and `atf suites` read stored benches/suites (a
  local YAML path still wins), matching the dashboard.

## Authoring a check

```bash
.venv/bin/atf new-check --id mgmt-tls-enum --vector mgmt --severity high \
  --title "Legacy TLS/protocols disabled"
```

produces a template you fill in:

```python
from atf.core.model import Ctx, Result, Severity, Verdict
from atf.core.registry import register

@register(id="mgmt-tls-enum", drivers=("mgmt",), actions=(),
          severity=Severity.HIGH, title="Legacy TLS/protocols disabled")
def mgmt_tls_enum(ctx: Ctx) -> Result:
    ev = ctx.write_evidence("raw probe output…")     # auto-named <id>-<board>.txt
    return Result(Verdict.GAP, title="…", detail="…", evidence=ev)
```

Then map it into a suite (in the dashboard's Studio, or a `suites/*.yaml`). Checks are
**decoupled from requirements** — the suite decides which requirement a check satisfies.

## AI

`atf` integrates with [Claude Code](https://claude.com/claude-code):

- **AI wizard** — from the dashboard you can connect an agent's machine to Claude and drive the
  framework in natural language (explore the catalog, author a check, run a suite, read a report)
  through an MCP server that exposes the framework's own API.
- **Resource pack** — connecting installs a pack (`CLAUDE.md`, skills, MCP server) so the agent's
  Claude knows how to use `atf`.

Developing the framework itself is also Claude-Code-first — see the dev pack below.

## Development

The repo root is a **Claude Code dev pack**: [`CLAUDE.md`](CLAUDE.md) is the engine guide and
[`.claude/skills/`](.claude/skills) has task workflows (run the dev server, add a driver, work on
the API/UI, evolve the store). Common commands:

```bash
make setup        # venv + editable install
make check        # compile + import sanity + ruff
make test         # regression suite (pytest; fast) · make test-all also runs the docker integration test
make web          # run the dashboard
make help         # all targets
```

## License

[MIT](LICENSE).
