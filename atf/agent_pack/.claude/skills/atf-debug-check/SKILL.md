---
name: atf-debug-check
description: Implement and iterate on an automated test — run just that one check against a board, read the raw evidence it produced, and refine until it verdicts correctly. Use when the developer is writing/fixing a .py test and wants the edit → run → inspect → fix loop.
---

# Debug a check (edit → run → read evidence → iterate)

The authoring loop for an automated `.py` test. Assumes the test already exists (see
`atf-author-driver-test`) and lives in a repo under `ATF_SOURCES`.

1. **Edit the implementation** directly in the file under `ATF_SOURCES` (Read/Edit). Keep it
   BLACK-BOX (probe from `ctx.<driver>`; never run commands on the board to measure it — see
   CLAUDE.md). Make the verdict decisive: `PASS` / `GAP` (a finding) / `ERROR`, with
   `ctx.write_evidence(...)` capturing what you observed.
2. **Run just this check** against a real board — fast, no full suite. Prefer the local backend for
   the tight loop:
   ```
   atf_benches()                     # pick a bench + board that wires the driver you need
   atf_run(bench="<bench>", board=["<board>"], ids=["<your-test-id>"], mgmt_backend="local")
   ```
   (`mgmt_backend="local"` skips the container — fastest for iterating; use `"docker"` to match the
   real mgmt vantage before you finish.)
3. **Read the result + the raw evidence:**
   ```
   atf_report(run_id="<run_id from atf_run>")     # verdict, detail, and each record's `evidence` path
   atf_evidence(run_id="<run_id>", path="<record.evidence>")   # what the probe actually saw
   ```
   Read the evidence, not just the verdict — it tells you whether the probe reached the target, what
   it observed, and why the verdict came out as it did.
4. **Interpret & fix:**
   - `skipped` → the board doesn't wire a **driver/action** your test declares. Don't change the
     test to dodge it — run against a board that provides it, or wire it on the bench.
   - `error` → a crash in the test; the evidence/detail carries the traceback. Fix the code.
   - Verdict wrong for what the evidence shows → adjust the pass/gap logic.
5. **Repeat** 1–4 until the verdict is right for a known-good and a known-bad target. Then confirm
   once with `mgmt_backend="docker"` (the real vantage) before mapping it into a suite
   (`atf-map-suite`).
