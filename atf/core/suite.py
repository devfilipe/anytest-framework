"""Suites — named, reusable test plans (the *what to run*), separate from the bench
(the *where*). A suite selects checks by requirement / id / vector and runs against any
bench:  `atf run --suite baseline --bench lab-a.yaml`. Lives in `suites/<name>.yaml`.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def load(name: str, suites_dir: str = "suites") -> dict:
    p = Path(suites_dir) / f"{name}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"suite not found: {p}")
    data = yaml.safe_load(p.read_text()) or {}
    sel = data.get("select") or {}
    return {
        "requirements": sel.get("req") or sel.get("requirements"),
        "ids": sel.get("ids"),
        "vectors": sel.get("vectors"),
        "title": data.get("title", name),
    }


def available(suites_dir: str = "suites") -> list[tuple[str, str]]:
    d = Path(suites_dir)
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.yaml")):
            try:
                title = (yaml.safe_load(p.read_text()) or {}).get("title", p.stem)
            except Exception:
                title = p.stem
            out.append((p.stem, title))
    return out
