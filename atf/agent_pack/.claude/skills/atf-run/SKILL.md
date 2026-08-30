---
name: atf-run
description: Run a atf suite or ad-hoc set of tests against a bench/board and read the report (verdicts per test, drivers/actions, skip reasons). Use when the developer wants to execute tests and interpret results.
---

# Run tests & read the report

Uses `$ATF_SERVER` + `$ATF_TOKEN` (injected when AI was turned on).

1. **See what's available** (catalog of tests, with drivers/actions):
   ```bash
   curl -s "$ATF_SERVER/api/agents/catalog" -H "authorization: Bearer $ATF_TOKEN"
   ```
2. **Run** — a saved suite, or ad-hoc by test ids. `board` is a list; `mgmt_backend` is `local` for dev:
   ```bash
   curl -s -X POST "$ATF_SERVER/api/run" -H "authorization: Bearer $ATF_TOKEN" \
     -H 'content-type: application/json' \
     -d '{"suite":"<suite>","bench":"<bench>","board":["<board>"],"mgmt_backend":"local"}'
   # ad-hoc: replace "suite" with  "ids":["host-recon","mgmt-open-ports"]
   ```
3. **Follow progress** (SSE): `curl -sN "$ATF_SERVER/api/run/stream" -H "authorization: Bearer $ATF_TOKEN"`
   — each `record` event has `check`, `verdict`, `drivers`, `actions`; `done` carries the counts.
4. **Read the report** (records + requirement×board roll-up):
   ```bash
   curl -s "$ATF_SERVER/api/reports/<run_id>" -H "authorization: Bearer $ATF_TOKEN"
   ```
   Interpret verdicts: `pass` / `gap` (finding) / `manual` (needs an operator) / `skipped`
   (a required **driver** or **action** isn't configured on this bench — the detail says which) /
   `error`. A requirement passes if and only if **all** its mapped tests passed on that board.
5. If a test was **skipped for a missing driver/action**, tell the developer to wire it on the
   bench (Studio › Benches → the board's Drivers / Node actions), not to change the test.
