"""Build `atf.pyz`: the client (`remote`, `mcp`, the agent pack's guided flows) as a zipapp of the code
this server runs, so a user downloads one file that matches the server — no clone, no install.
Standard library only at run time (YAML benches need pyyaml; JSON always works)."""
from __future__ import annotations

import io
import zipfile
from functools import lru_cache
from pathlib import Path

_ATF = Path(__file__).resolve().parent
_FILES = ("__init__.py", "remote.py", "mcp.py", "client.py", "agent_pack/atf_mcp.py")
_MAIN = "import sys\nfrom atf.client import main\nsys.exit(main())\n"


@lru_cache(maxsize=1)
def build() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("__main__.py", _MAIN)
        for rel in _FILES:
            z.write(_ATF / rel, f"atf/{rel}")
        z.writestr("atf/agent_pack/__init__.py", "")   # a regular package: zipimport and namespace dirs disagree
    return b"#!/usr/bin/env python3\n" + buf.getvalue()
