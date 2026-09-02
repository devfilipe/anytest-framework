#!/usr/bin/env python3
"""PostToolUse advisory for Write/Edit: after the Wizard writes a Python test under atf_checks/,
byte-compile it and, if it doesn't parse, surface the error so the Wizard fixes it immediately
(a broken .py would break the agent's whole catalog on discovery). PostToolUse can't undo the write,
but exit 2 shows this stderr to the model. Only .py files under atf_checks/ are checked; stdlib only."""
import json
import os
import py_compile
import sys

try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)

fp = (data.get("tool_input") or {}).get("file_path", "")
if not (fp.endswith(".py") and "atf_checks" in fp) or not os.path.isfile(fp):
    sys.exit(0)

try:
    py_compile.compile(fp, doraise=True)
except py_compile.PyCompileError as e:
    sys.stderr.write(f"atf: {os.path.basename(fp)} does not compile — fix it before running:\n{e.msg}\n")
    sys.exit(2)
sys.exit(0)
