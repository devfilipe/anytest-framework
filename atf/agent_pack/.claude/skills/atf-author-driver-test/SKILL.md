---
name: atf-author-driver-test
description: Author a new automated atf test (.py) that probes a board black-box through a driver (mgmt/console/craft/host) and optionally a node action. Use when the developer wants to create or draft an automated pentest check.
---

# Author an automated atf test

1. **Decide the contract** with the developer:
   - `id` (kebab-case, e.g. `mgmt-tls-enum`), `title`, `model` (`common` or a slug like `router-x`).
   - **drivers** it needs: any of `mgmt`, `console`, `craft` (none ⇒ host-only). Declared in
     `@register` — it does NOT affect the file path.
   - **actions** it needs (optional): e.g. `power-cycle`.
   - `severity` of a gap.
2. **Scaffold the file** so it lands in the agent's repo with the SDK skeleton. **Prefer the
   `atf_scaffold` MCP tool** (it reads the server + token from the environment — never hardcode
   credentials):
   ```
   atf_scaffold(kind="auto", id="mgmt-tls-enum", source="<repo>", model="common",
                drivers=["mgmt"], actions=[], severity="medium", title="Legacy TLS enumerated")
   ```
   (`<repo>` = a source repo the agent serves.) This writes `atf_checks/<model>/<id>.py` — flat
   under the model — and refuses (409) if that id already exists (it won't overwrite).
   Raw-API fallback (debug only): `POST $ATF_SERVER/api/agents/$AID/scaffold` with the same fields.
3. **Implement the black-box logic** in that file. Enforce THE BLACK-BOX RULE (see CLAUDE.md):
   probe from `ctx.<driver>` — never run commands on the board to measure it. The sanctioned
   exception is an adversarial "try to break in; success ⇒ GAP" test.
4. Return a `Result(Verdict.…, title, detail, evidence=ctx.write_evidence(...))`.
5. **Do not** add requirements coupling — the Suite maps requirement↔test. At most keep an advisory
   `requirements=(...)` hint.
6. Tell the developer to map it in a Suite and run it (see the `atf-run` skill).
