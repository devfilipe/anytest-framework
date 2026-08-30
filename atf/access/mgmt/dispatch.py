"""Host-side dispatcher: send mgmt checks to the atf-mgmt container (or run locally)."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from atf.core.inventory import Board
from atf.core.model import CheckSpec


def _build_request(board: Board, check_ids: list[str]) -> dict:
    # only the ip-like drivers without an agent run in the container (the host/nmap vantage)
    drivers = {alias: cfg for alias, cfg in (board.drivers or {}).items()
               if isinstance(cfg, dict) and cfg.get("type") != "serial" and not cfg.get("agent")}
    return {
        "board": {
            "name": board.name, "model": board.model, "serial": board.serial,
            "drivers": drivers,
            "creds": {r: {"user": c.user, "password": c.password}
                      for r, c in board.creds.items()},
        },
        "checks": check_ids,
        "options": {},
    }


def _err_results(check_ids: list[str], msg: str) -> list[dict]:
    return [{"check": cid, "requirements": [], "drivers": [], "actions": [], "verdict": "error",
             "severity": "info", "title": "driver dispatch failed", "detail": msg[:800],
             "evidence": "", "metrics": {}} for cid in check_ids]


def _docker(request: dict, out_root: Path, image: str) -> dict:
    out_root.mkdir(parents=True, exist_ok=True)
    cmd = ["docker", "run", "--rm", "--network", "host", "-i",
           "-v", f"{out_root.resolve()}:/out"]
    # mount the check-source repos (checks are not baked into the image) + point discovery at them
    from atf.core.checks import source_paths
    mounts = []
    for i, src in enumerate(source_paths()):
        cmd += ["-v", f"{src.resolve()}:/checks/{i}:ro"]
        mounts.append(f"/checks/{i}")
    if mounts:
        cmd += ["-e", "ATF_CHECK_SOURCES=" + ":".join(mounts)]
    cmd += [image, "atf", "_mgmt-worker", "--out", "/out"]
    if hasattr(os, "getuid"):  # keep evidence owned by the caller, not container root
        cmd[3:3] = ["--user", f"{os.getuid()}:{os.getgid()}"]
    try:
        p = subprocess.run(cmd, input=json.dumps(request),
                           capture_output=True, text=True, timeout=1800)
    except Exception as e:  # docker missing / timeout
        return {"results": _err_results(request["checks"], f"docker run error: {e}")}
    if p.returncode != 0:
        return {"results": _err_results(request["checks"],
                                        f"rc={p.returncode}: {p.stderr}")}
    try:
        return json.loads(p.stdout)
    except Exception as e:
        return {"results": _err_results(request["checks"],
                                        f"bad worker json: {e}; stdout={p.stdout[:300]}")}


def _local(request: dict, out_root: Path) -> dict:
    from atf.access.mgmt import worker as mgmt_worker
    return mgmt_worker.run(request, out_root)


def dispatch(board: Board, specs: list[CheckSpec], out_root: Path,
             backend: str = "docker", image: str = "atf-mgmt:latest") -> list[dict]:
    ids = [s.id for s in specs]
    request = _build_request(board, ids)
    resp = _local(request, out_root) if backend == "local" else _docker(request, out_root, image)
    return resp.get("results", [])
