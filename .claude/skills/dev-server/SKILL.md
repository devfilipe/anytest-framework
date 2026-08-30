---
name: dev-server
description: Run the Anytest Framework API + dashboard locally against the bundled example check-source, and exercise the CLI. Use when developing/testing the engine and you need it running.
---

# Run the dev server

The engine needs a check-source on `ATF_CHECK_SOURCES` to have anything to run. Use the bundled
`examples/` set (one `host-recon` check + a localhost bench).

## First time

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[host,web]"
```

## Environment

```bash
export APP_SECRET=dev-secret            # at-rest key for stored secrets; any stable value in dev
export ATF_CHECK_SOURCES="$PWD/examples"
```

- `APP_SECRET` must stay constant across runs or previously-stored secrets can't be decrypted.
- `ATF_CHECK_SOURCES` is `:`-separated repo roots, each containing an `atf_checks/` dir.

## CLI loop

```bash
.venv/bin/atf list                       # discovered checks (expect: host-recon)
.venv/bin/atf run --id host-recon \
  --bench examples/benches/lab.yaml --mgmt-backend local     # → verdicts: {'pass': 1}
.venv/bin/atf report                     # rebuild matrix/findings from the last run
```

## Web

```bash
.venv/bin/atf web                        # http://127.0.0.1:8899  (login admin/admin)
```

- API docs: <http://127.0.0.1:8899/api/docs> (open, no auth). Data endpoints need a login token.
- The server is long-running. Start it detached (`setsid … < /dev/null &`) so it survives the
  shell, and stop the old one before restarting (only one process can bind `:8899`).
- Startup prints `upstream checks available: N` — a quick discovery sanity check.

## Verify a change

```bash
make check      # compileall + `atf list` import sanity + ruff
```
