# CLAUDE.md — Anytest Framework engine

The guidance that used to live here now lives in a deck pack, versioned apart
from this repository so it can be reviewed on its own and shared with
`anytest-checks-common`, which holds the same conventions and the same commit
rules.

    ~/…/anytest-framework-ai-packs/packs/
      _common/                   how a commit is shaped, what the workspace is,
                                 no vendor names in either repository
      anytest-framework/         nine invariants about this engine, plus the
                                 add-driver, dev-server, store-schema and
                                 web-api skills
      anytest-checks-common/     what a check may and may not do

Bring it into this working tree:

```bash
export DECK_ROOT=~/workspace/anytest
deck mount --task <what you are doing>
deck unmount --task <the same>
```

Mounted rules land in `.claude/rules/` as `deck-*.md` and are removed on
unmount; the manifest is in `.deck/mounts/`. `deck` never deletes what it did
not place.

Why not here: a rule in this file is loaded on every turn whatever the change,
and it drifts from the repository it describes with nothing to catch it. A rule
in a pack carries `paths:` — it is loaded when a matching file is read and not
otherwise — and the pack's gates are run, so what a machine can check is
checked rather than asserted.

**The architecture, the dev quickstart and the conventions are in `README.md`
and `CONTRIBUTING.md`**, which are for people and were never Claude's to read
alone.
