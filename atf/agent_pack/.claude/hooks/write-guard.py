#!/usr/bin/env python3
"""PreToolUse guard for Write/Edit: the Wizard may only write inside a configured check-source repo
(ATF_SOURCES), the AI pack dir, or a temp dir. A write anywhere else (home dotfiles, /etc, the
framework, another repo) is blocked (exit 2) — the Wizard's job is authoring tests & requirements in
the tester's own repos, not touching the rest of the machine.

Allowed roots cover the WHOLE repo tree (so both atf_checks/ and requirements/ are fine). Reads the
tool's file_path from the hook JSON on stdin; stdlib only."""
import json
import os
import sys
import tempfile

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)                                     # can't parse → don't block

fp = ((data.get("tool_input") or {}).get("file_path")
      or (data.get("tool_input") or {}).get("path") or "")
if not fp:
    sys.exit(0)

target = os.path.realpath(os.path.expanduser(fp))
roots = [os.path.realpath(os.path.expanduser(p))
         for p in os.environ.get("ATF_SOURCES", "").split(os.pathsep) if p]
roots.append(os.path.realpath(os.path.join(os.path.dirname(__file__), "..", "..")))   # the AI pack
roots.append(os.path.realpath(tempfile.gettempdir()))

if any(target == r or target.startswith(r + os.sep) for r in roots):
    sys.exit(0)

sys.stderr.write(
    "blocked by atf: the Wizard may only write inside your check-source repos (ATF_SOURCES), the AI "
    f"pack, or a temp dir — not {target}. Author tests under atf_checks/ and requirements under "
    "requirements/ in one of your repos.\n")
sys.exit(2)
