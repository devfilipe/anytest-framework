---
name: atf-author-requirement
description: Author or edit a requirement catalog (a framework's requirements) as a YAML file in a check-source repo. Use when the developer wants to add/change the requirements a suite maps tests to.
---

# Author a requirement catalog (YAML)

Requirements are the *what must hold* — the left-hand side of a suite's map. They live as a YAML
catalog in a check-source repo the agent serves, at **`requirements/<framework>.yaml`** (NOT under
`atf_checks/` — that's tests). One file per framework.

1. **See what exists** so you extend rather than duplicate: `atf_requirements()` lists the
   frameworks; `atf_requirements(framework="<fw>")` lists the requirements in one.
2. **Locate/create the file** in a repo under `ATF_SOURCES`: `requirements/<framework>.yaml`. Edit it
   directly (Read/Edit/Write) — same as tests are edited in place.
3. **Format** (canonical):
   ```yaml
   framework: vivo
   title: Vivo security baseline
   requirements:
     - code: C.4
       title: Management plane reachable only over the DCN
       description: The board answers on its DCN/mgmt address and no other.
       verify: Probe the mgmt vantage — reachable on the DCN, and only expected ports open.
       priority: 0
     - code: E.3
       title: No legacy TLS on management services
       description: Management TLS offers only modern protocols/ciphers.
       verify: Enumerate TLS from the mgmt vantage; a legacy protocol/cipher is a gap.
       priority: 1
   ```
   - `code` is the requirement id within the framework (referenced in a suite as `<framework>:<code>`,
     e.g. `vivo:C.4`).
   - `verify` describes HOW to check it black-box — this is the bridge to the test(s) a suite maps.
   - `priority` is optional (used for ordering/triage).
4. **Keep requirements black-box and testable** — phrase `verify` as an external observation
   ("enumerate…", "probe…", "attempt…"), not "read the config on the device".
5. Do **not** couple a requirement to a specific test here — the **Suite** maps requirement↔test
   (see `atf-map-suite`). A requirement can be proven by one or many tests, chosen in the suite.
6. After saving, the catalog is picked up as the agent's overlay; confirm with
   `atf_requirements(framework="<fw>")`, then map tests to it with `atf-map-suite`.
