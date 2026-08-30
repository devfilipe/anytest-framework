---
name: atf-author-manual-test
description: Author a manual atf test as a Markdown artifact (.md) — the operator-run steps + pass/gap verdict, for checks with no black-box automation. Use when the developer wants a guided manual procedure.
---

# Author a manual atf test (Markdown)

A manual test is a Markdown artifact under `atf_checks/<model>/manual/<id>.md` — frontmatter +
the steps an operator runs. It is run as `mode=manual` (an operator captures the verdict); in a
headless run it is reported `manual`/skipped.

1. **Scaffold** it on the agent repo:
   ```bash
   curl -s -X POST "$ATF_SERVER/api/agents/$AID/manual" \
     -H "authorization: Bearer $ATF_TOKEN" -H 'content-type: application/json' \
     -d '{"id":"manual-uart-photo","source":"<repo>","model":"router-x","drivers":[],"actions":[],
          "severity":"high","title":"Debug interface photo"}'
   ```
   Drivers/actions are usually empty (operator-driven ⇒ always available); add them only to gate
   the run on a channel (e.g. `["power-cycle"]` if the procedure needs a bench power-cycle).
2. **Write the steps** in Markdown, black-box, with a clear verdict. Structure:
   `## Objetivo` · `## Pré-condições` · `## Passos` (numbered) · `## Observações` (checklist `- [ ]`)
   · `## Veredito` (when is it **pass**, when **gap**). Include known gaps to look for.
3. Keep the operator honest: describe what to *observe*, not board commands to run as a measurement.
4. Tell the developer to map it in a Suite and run it (see `atf-run`).
