# Contributing to Anytest Framework

Thanks for helping! This repo is the **engine** (`atf/`) — the runner, access channels, config
store, web API/UI and CLI. Checks, benches, suites and requirement catalogs live in *separate*
check-source repos; the engine ships only a tiny runnable `examples/` set.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[host,web]" ruff
```

## Dev loop

```bash
export APP_SECRET=dev-secret ATF_CHECK_SOURCES="$PWD/examples"
.venv/bin/atf list                                              # discovered checks
.venv/bin/atf run --id host-recon --bench examples/benches/lab.yaml --mgmt-backend local
.venv/bin/atf web                                               # http://127.0.0.1:8899
make check                                                      # compile + import sanity + ruff
make test                                                       # regression suite (fast; needs the .[test] extra)
```

## Tests

The regression suite lives in `tests/` (pytest). Install the extra once: `pip install -e '.[test]'`.

```bash
make test        # fast suite — store/migration, SDK, channels, API (no docker)     — CI runs this on 3.10–3.12
make test-all    # also the integration test vs anytest-checks-common (needs Docker + the sibling repo)
```

The integration test (`-m integration`) brings up the vulnerable target from
`anytest-checks-common` and asserts the baseline suite detects it; CI runs it in a separate job
that checks out both repos. Run `make check` **and** `make test` before opening a PR.

## Conventions

- **Python**: [Ruff](https://docs.astral.sh/ruff/), line length 120, double quotes, spaces. No
  unrelated reformatting in a PR.
- **The engine is vendor-agnostic.** No product or vendor names anywhere in `atf/`. Keep examples
  generic (`router-x`, `acme:E.3`, `board-1`).
- **Checks declare capabilities, not requirements.** `@register(id, drivers=(), actions=(), …)` —
  never a `requirements=` argument. The *suite* owns the requirement→test map.
- **Drivers & actions are the SDK.** A check declaring a driver/action the bench lacks is
  **skipped**, never hard-failed.
- **Security checks are black-box by default** — probe from outside, don't lean on device logins as
  a measurement shortcut. This is a check-authoring convention, not an engine constraint; the engine
  supports any kind of test.
- **The config store is the source of truth**; YAML only seeds/imports/exports it. Secrets are
  always encrypted via `APP_SECRET` — never log or export plaintext. Schema changes touch both
  `store/schema.sql` and `store/repo.py`.
- **The dashboard** (`web/static/index.html`) is a single self-contained file — vanilla JS, inline
  CSS, no build step, no external requests. Keep it that way; escape data with `esc()`.
- **Auth**: every `/api/*` route is behind the login gate unless explicitly allow-listed in
  `server.py` `_login_gate`. Gate admin-only actions with `require_admin`.

## Claude Code

The repo is Claude-Code-first, and the guidance lives in a deck pack rather
than in this repository — versioned apart so it can be reviewed on its own and
shared with `anytest-checks-common`, which holds the same conventions.

The pack carries nine invariants about this engine and four task workflows
(dev-server, add-driver, web-api, store-schema). [`CLAUDE.md`](CLAUDE.md) says
where it is and how to bring it into a working tree:

```bash
export DECK_ROOT=~/workspace/anytest
deck mount --task <what you are doing>
```

You don't need Claude Code, or deck, to contribute. The rules are prose and the
workflows are readable where they live; what deck adds is that they arrive when
they are relevant and leave when they are not, and that what a machine can
check is a gate rather than a paragraph.

## Pull requests

1. Branch from `main`.
2. Keep the change focused; update `examples/` when behaviour changes.
3. `make check` passes and the example run works.
4. Describe *what* and *why* in the PR. Reference an issue if there is one.

By contributing you agree your contributions are licensed under the [MIT License](LICENSE).
