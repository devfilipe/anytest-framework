"""Requirement catalogs. The DB (config store) is the source of truth; this module reads
it host-side — used by the report/matrix to enrich findings — with a YAML-file fallback so
the CLI works without the web/DB running. Format is YAML (framework + list); the old TSV/txt
format is retired. The mgmt worker never imports this (it only needs requirement *ids*).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

_CACHE: dict[str, dict] = {}


def requirement_sha(row: dict) -> str:
    """Content signature of a requirement (title|desc|verify|priority) → the provenance token a
    suite map stores per referenced requirement. Same shape as a check's file-content `sha`
    (sha1[:12]); drift = the requirement text changed since it was mapped."""
    body = "|".join(str(row.get(k, "") or "") for k in ("title", "desc", "verify", "priority"))
    return hashlib.sha1(body.encode()).hexdigest()[:12]


def parse_yaml(text: str):
    """A catalog YAML -> (framework, title, {code: {title, desc, verify, priority}})."""
    data = yaml.safe_load(text) or {}
    out = {}
    for r in (data.get("requirements") or []):
        code = r.get("code")
        if not code:
            continue
        pr = r.get("priority")
        out[code] = {"title": r.get("title", ""), "desc": r.get("description", ""),
                     "verify": r.get("verify", ""),
                     "priority": str(pr) if pr is not None else None}
    return data.get("framework", ""), data.get("title", ""), out


def dump_yaml(framework: str, title: str, rows: list[dict]) -> str:
    """rows = [{code,title,desc,verify,priority}] -> canonical catalog YAML."""
    doc = {"framework": framework, "title": title,
           "requirements": [{"code": r["code"], "title": r.get("title", ""),
                             "description": r.get("desc", ""), "verify": r.get("verify", ""),
                             "priority": r.get("priority")} for r in rows]}
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def _from_db(framework: str):
    try:
        from atf.store import open_repo
        rows = open_repo().list_requirements(framework)
        if rows:
            return {r["code"]: {"title": r["title"], "desc": r["desc"],
                                "verify": r["verify"], "priority": r["priority"]} for r in rows}
    except Exception:
        pass
    return None


def _from_file(framework: str, requirements_dir: str):
    p = Path(requirements_dir) / f"{framework}.yaml"
    return parse_yaml(p.read_text())[2] if p.exists() else {}


def catalog(framework: str, requirements_dir: str = "requirements") -> dict:
    """Parsed `{code: {title, desc, verify, priority}}` — DB first, YAML fallback. Cached."""
    if framework in _CACHE:
        return _CACHE[framework]
    meta = _from_db(framework)
    if meta is None:
        meta = _from_file(framework, requirements_dir)
    _CACHE[framework] = meta
    return meta


def invalidate() -> None:
    """Drop the cache after a requirement edit so the report picks up fresh metadata."""
    _CACHE.clear()


def describe(req_id: str, requirements_dir: str = "requirements") -> dict:
    if ":" not in req_id:
        return {}
    fw, code = req_id.split(":", 1)
    return catalog(fw, requirements_dir).get(code, {})
