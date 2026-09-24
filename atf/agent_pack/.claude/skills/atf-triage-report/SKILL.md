---
name: atf-triage-report
description: Read and interpret a run's report — separate real findings (gap) from skips and errors, roll up per requirement, read the evidence behind each, and recommend next actions. Use when the developer wants to understand results or write up findings.
---

# Triage a report

Turn a run into an actionable read: what's a real finding, what didn't run and why, and what to do.

1. **Load the report:** `atf_report(run_id="<run_id>")`. It has per-test records (verdict, drivers,
   actions, detail, `evidence`, source version) plus the requirement×board roll-up.
2. **Classify each record's verdict:**
   - `pass` — the check ran and the target met it.
   - `gap` — a **real finding** (the security/compliance issue the check looks for). This is the
     signal; dig into it.
   - `manual` — needs an operator to capture the verdict (a Markdown manual test in a headless run).
   - `skipped` — a required **driver or action isn't wired** on this bench for that board (the detail
     says which). NOT a finding — it means the run couldn't exercise it here.
   - `error` — the check crashed; treat as unreliable, not as pass/gap.
3. **Read the evidence behind gaps and errors** — don't trust the one-line detail alone:
   `atf_evidence(run_id="<run_id>", path="<record.evidence>")`. Quote the concrete observation
   (open port, weak cipher, ICMP reply, root granted, …) when you write the finding.
4. **Roll up per requirement** using the report's requirement×board matrix: a requirement **passes
   iff every mapped test passed** on that board; any gap/error/skip (or a mapped test that didn't
   run) makes it fail. Report which requirements are met, which have findings, and which are
   inconclusive (skips) on which boards.
5. **Recommend next actions**, distinguishing:
   - **Findings (gap)** → describe the exposure + severity; suggest the fix on the *device*, not the
     test.
   - **Skips** → wire the missing driver/action on the bench (Studio › Benches), or run against a
     board that has it — don't weaken the test.
   - **Errors** → fix the check (use `atf-debug-check`).
   - **Drift** (if a suite validation flagged it) → the test/requirement changed since it was mapped;
     re-map or re-baseline.

Be black-box and factual: report what the evidence shows, cite the requirement it maps to, and never
infer a pass from a skip.
