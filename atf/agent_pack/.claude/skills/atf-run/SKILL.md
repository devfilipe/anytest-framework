---
name: atf-run
description: Run a atf suite or ad-hoc set of tests against a bench/board and read the report (verdicts per test, drivers/actions, skip reasons). Use when the developer wants to execute tests and interpret results.
---

# Run tests & read the report

**Prefer the `atf` MCP tools** (they read `$ATF_SERVER` + `$ATF_TOKEN` from the environment — never
hardcode the token). Raw `curl` equivalents are shown only as a debug fallback.

1. **See what's available** (catalog of tests, with drivers/actions): `atf_catalog()`.
   (raw: `GET $ATF_SERVER/api/agents/catalog`)
2. **Run** — a saved suite, or ad-hoc by test ids. `board` is a list; `mgmt_backend` is `local` for dev:
   ```
   atf_run(suite="<suite>", bench="<bench>", board=["<board>"], mgmt_backend="local")
   # ad-hoc: drop `suite`, pass  ids=["host-recon","mgmt-open-ports"]
   ```
   A run only ever executes on the **caller's own agent** — the report is attributed to you, and a
   suite that resolves to zero tests comes back with a note saying why (id/model/source).
   (raw: `POST $ATF_SERVER/api/run` with `{suite|ids, bench, board, mgmt_backend}`)
3. **Follow progress** (optional, SSE): `curl -sN "$ATF_SERVER/api/run/stream"` — each `record`
   event has `check`, `verdict`, `drivers`, `actions`; `done` carries the counts (and any note).
4. **Read the report** (records + requirement×board roll-up): `atf_report(run_id="<run_id>")`.
   (raw: `GET $ATF_SERVER/api/reports/<run_id>`)
   Interpret verdicts: `pass` / `gap` (finding) / `manual` (needs an operator) / `skipped`
   (a required **driver** or **action** isn't configured on this bench — the detail says which) /
   `error`. A requirement passes if and only if **all** its mapped tests passed on that board.
5. If a test was **skipped for a missing driver/action**, tell the developer to wire it on the
   bench (Studio › Benches → the board's Drivers / Node actions), not to change the test.
