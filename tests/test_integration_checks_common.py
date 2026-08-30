"""End-to-end regression against the anytest-checks-common repo: bring up its deliberately-
vulnerable target (an unauthenticated Redis) and confirm the baseline suite detects it.

Needs Docker + the anytest-checks-common repo checked out as a sibling. Marked `integration`
(run with `pytest -m integration` or `make test-all`; skipped otherwise / in the fast suite)."""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent.parent / "anytest-checks-common"

pytestmark = pytest.mark.integration


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not REPO.is_dir(), reason="anytest-checks-common not checked out as a sibling")
@pytest.mark.skipif(not _docker_ok(), reason="docker compose not available")
def test_baseline_suite_detects_vulnerable_redis(tmp_path):
    env = {**os.environ, "APP_SECRET": "ci-secret", "ATF_CHECK_SOURCES": str(REPO)}
    subprocess.run(["docker", "compose", "up", "-d"], cwd=REPO, check=True, capture_output=True)
    try:
        out = tmp_path / "out"
        r = subprocess.run(
            [sys.executable, "-m", "atf.cli", "run", "--suite", "baseline",
             "--bench", "benches/localhost.yaml", "--mgmt-backend", "local", "--out", str(out)],
            cwd=REPO, env=env, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, f"run failed: {r.stderr}\n{r.stdout}"
        recs = json.loads((out / "results.json").read_text())
        recs = recs if isinstance(recs, list) else recs.get("records", [])
        verdicts = {x["check"]: x["verdict"] for x in recs}
        # host-open-ports (host TCP probe, no nmap needed) must flag the unauthenticated Redis on :6379
        assert verdicts.get("host-open-ports") == "gap", f"verdicts={verdicts}"
    finally:
        subprocess.run(["docker", "compose", "down"], cwd=REPO, capture_output=True)
