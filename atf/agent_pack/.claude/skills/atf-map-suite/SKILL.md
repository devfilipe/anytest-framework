---
name: atf-map-suite
description: Build or edit a Suite — the ordered requirement→test map that says what a run checks. Use when the developer wants to assemble/curate a suite, map tests to requirements, or fix a suite that references a missing/drifted test.
---

# Map a Suite (requirement → test)

A **Suite** is the map from requirements to the tests that prove them — ordered, and separate from
the bench. A **Test Plan** later pairs a suite with a bench/board. Author it with the MCP tools;
never hand-map with raw curl.

1. **Decide the target model** with the developer: `common` (any board) or a model slug (e.g.
   `tmd400g`). Model-specific tests/requirements only apply to that model.
2. **Gather the pieces:**
   - Requirements: `atf_requirements()` for the catalogs, `atf_requirements(framework="…")` for the
     requirements in one. Pick the ones the suite must cover.
   - Tests: `atf_catalog()` — note each test's `id`, `drivers`, `actions`, `model`.
3. **Draft the map** with the developer — for each requirement, the ordered list of test ids that
   prove it. Rules:
   - **Order = run order** (list order): put setup first, teardown last.
   - A requirement **passes iff every mapped test passed** on that board.
   - A requirement with **no test** falls back to `TEST_PASS` (placeholder pass) or `TEST_FAIL`
     (marks a gap so it isn't a silent hole) — ask which.
   - Map **by test id** (source-agnostic): at run time the running user's own agent provides the
     implementation.
4. **Save + validate** in one call:
   ```
   atf_map(name="baseline", model="tmd400g", title="…",
           requirements=[
             {"id": "vivo:C.4", "tests": ["ping-dcn"], "fallback": "TEST_FAIL"},
             {"id": "vivo:E.3", "tests": ["mgmt-tls-legacy"]},
             {"id": "vivo:F.1", "tests": [], "fallback": "TEST_FAIL"}
           ])
   ```
   The result includes `validation`: every referenced requirement/test is checked for
   `missing` (not loaded) or `drift` (the test/requirement changed since you mapped it). Resolve any
   `missing`/`drift` before running — a missing test means the agent that provides it isn't
   connected, or the id is wrong.
5. Hand off to the **atf-run** skill to run the suite against a bench/board and read the report.
