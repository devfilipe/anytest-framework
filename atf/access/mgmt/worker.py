"""Executed INSIDE the atf-mgmt container (entrypoint `atf _mgmt-worker`).

Reads a request (board + creds + ip-driver configs + check ids) from stdin, runs the checks with
a real IpChannel per driver alias, and emits the response JSON on stdout. Same Check/Result model
as the host — the boundary is just this JSON (see DESIGN.md §3a). Also used by the `local`
dispatch backend (called in-process, no docker).
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def run(request: dict, out_root) -> dict:
    from atf.core.checks import discover
    discover()  # import checks from the mounted check-source repos ($ATF_CHECK_SOURCES)
    from atf.access.channels.ip import IpChannel
    from atf.access.host import HostProbe
    from atf.core.inventory import Board, Creds
    from atf.core.model import Ctx
    from atf.core.registry import REGISTRY
    from atf.core.runner import result_dict, run_check

    b = request["board"]
    creds = {r: Creds(**c) for r, c in (b.get("creds") or {}).items()}
    board = Board(name=b["name"], model=b.get("model", ""), serial=b.get("serial", ""),
                  creds=creds, drivers=b.get("drivers") or {})
    # one IpChannel per driver in the request (all are ip-without-agent → host/container vantage)
    drivers = {alias: IpChannel(cfg, None, creds) for alias, cfg in (b.get("drivers") or {}).items()}
    ctx = Ctx(board=board, host=HostProbe(), out_root=Path(out_root), drivers=drivers)

    started = time.time()
    results = []
    for cid in request.get("checks", []):
        spec = REGISTRY.get(cid)
        if spec is None:
            results.append({"check": cid, "requirements": [], "drivers": [], "actions": [],
                            "verdict": "error", "severity": "info",
                            "title": f"unknown check {cid}", "detail": "",
                            "evidence": "", "metrics": {}})
            continue
        results.append(result_dict(spec, run_check(spec, ctx)))
    for ch in drivers.values():
        ch.close()

    return {"worker": {"image": os.environ.get("ATF_IMAGE", "local"),
                       "duration_s": round(time.time() - started, 1)},
            "results": results, "errors": []}
