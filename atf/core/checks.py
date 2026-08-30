"""Discover check code from the configured check-source repos.

Checks live OUTSIDE the framework, in one or more repos that each contribute a portion of the
``atf_checks`` namespace package: a common repo (``atf_checks/common/…``) and per-model repos
(``atf_checks/<slug>/…``). A "check source" is a directory that contains such a ``atf_checks/``.

Sources come from ``$ATF_CHECK_SOURCES`` (``os.pathsep``-separated absolute paths) or, for dev,
are auto-detected as sibling clones next to the framework checkout. Importing every module under
the merged ``atf_checks`` namespace runs each ``@register`` — no central import list.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import sys
from pathlib import Path

_DEV_SIBLINGS = ("anytest-checks-common", "anytest-checks-router-x")


def checkout_base() -> Path:
    return Path(os.environ.get("ATF_CHECKOUTS", "checkouts")).resolve()


def source_paths() -> list[Path]:
    """Resolve the check-source repo roots. ONLY explicitly-provided sources count — the app never
    auto-discovers anything, so with nothing configured there is no upstream and it depends on
    connected agents:
      1. $ATF_CHECK_SOURCES (explicit, dev/CI/container override);
      2. DB-configured check_source repos → their synced checkouts (Admin › Repositories).
    No configured repo → no upstream checks/requirements."""
    env = os.environ.get("ATF_CHECK_SOURCES", "")
    if env.strip():
        return [Path(p) for p in env.split(os.pathsep) if p.strip()]
    try:
        from atf.store import open_repo
        srcs = open_repo().list_check_sources()
    except Exception:
        srcs = []
    base = checkout_base()
    out = []
    for s in srcs or []:
        if not s.get("enabled"):
            continue
        # a `path` source's checkout IS the configured dir; a `git` source clones into base/name
        co = Path(s["checkout"]) if s.get("checkout") else base / s["name"]
        if (co / "atf_checks").is_dir():
            out.append(co)
    return out


def source_roots() -> list[tuple[str, str]]:
    """`(resolved source path, label)` for each loaded check source. The label is the configured
    check_source repo name for a synced checkout, else the directory name (dev siblings / an
    explicit `$ATF_CHECK_SOURCES`)."""
    labels: dict[str, str] = {}
    try:
        from atf.store import open_repo
        for s in open_repo().list_check_sources():
            co = s.get("checkout")
            if co:
                labels[str(Path(co).resolve())] = s["name"]
    except Exception:
        pass
    out = []
    for p in source_paths():
        rp = str(p.resolve())
        out.append((rp, labels.get(rp, p.name)))
    return out


def _rev(path: Path) -> str:
    """Short git sha of a checkout (empty if it isn't a git repo)."""
    return _git(path, ["rev-parse", "--short", "HEAD"])


def _git(path: Path, args: list[str]) -> str:
    import subprocess
    try:
        r = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def source_versions() -> list[dict]:
    """Provenance of each loaded check source AS LOADED: `{name, path, commit, ref, dirty}` from git
    (best-effort — a non-git source reports commit=''). This is the version the report records so a
    result can be traced to the exact revision of the checks/requirements that produced it."""
    out = []
    for root, label in source_roots():
        p = Path(root)
        commit = _git(p, ["rev-parse", "--short", "HEAD"])
        ref = _git(p, ["rev-parse", "--abbrev-ref", "HEAD"]) if commit else ""
        dirty = bool(_git(p, ["status", "--porcelain"])) if commit else False
        out.append({"name": label, "path": root, "commit": commit,
                    "ref": "" if ref == "HEAD" else ref, "dirty": dirty})
    return out


def sync_sources(repo, by: str = "") -> list[dict]:
    """Load each enabled check_source. `kind=git` clones/fetches into the checkout base (SSH URLs use
    the ambient key — no credential is stored; HTTPS may use an encrypted token). `kind=path` just
    validates a server-local dir (no clone). Records the loaded sha1 + `by` (the user who synced).
    Tolerant: on any failure it records status='error' and contributes nothing (never raises)."""
    import subprocess
    from datetime import datetime
    base = checkout_base()
    base.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND":
           "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new -oConnectTimeout=10"}
    results = []
    for src in repo.list_check_sources():
        name, ref = src["name"], (src.get("ref") or "").strip()
        kind = src.get("kind") or "git"
        now = datetime.now().isoformat(timespec="seconds")
        if not src.get("enabled"):
            repo.set_check_source_status(name, "disabled", "", None, now, by=by)
            results.append({"name": name, "status": "disabled"})
            continue
        if kind == "path":                              # server-local directory — no clone
            d = Path(os.path.expanduser(src["url"]))
            if (d / "atf_checks").is_dir():
                status, msg, ck = "ok", f"loaded {d}", str(d.resolve())
            elif d.is_dir():
                status, msg, ck = "error", "no atf_checks/ in that directory", None
            else:
                status, msg, ck = "error", "directory not found on the server", None
            commit = _rev(d) if status == "ok" else ""
            repo.set_check_source_status(name, status, msg, ck, now, commit=commit, by=by)
            results.append({"name": name, "status": status, "message": msg})
            continue
        dest = base / name
        url = src["url"]
        # private HTTPS (GitHub/GitLab/…): inject a token as the userinfo. git redacts creds in
        # its own errors; we also scrub below so a token never lands in a stored status message.
        token = repo.get_check_source_token(name) if hasattr(repo, "get_check_source_token") else ""
        auth_url = url
        if token and url.startswith("https://"):
            auth_url = "https://" + token + "@" + url[len("https://"):]

        def _git(args, timeout):
            return subprocess.run(["git", *args], env=env, capture_output=True,
                                  text=True, timeout=timeout, check=True)

        def _scrub(s):
            return s.replace(token, "***") if token else s

        try:
            if (dest / ".git").is_dir():
                # fetch from the (possibly tokenized) url explicitly so the token isn't persisted
                if ref:
                    _git(["-C", str(dest), "fetch", "--depth", "1", auth_url, ref], 90)
                else:
                    _git(["-C", str(dest), "fetch", "--depth", "1", auth_url], 90)
                _git(["-C", str(dest), "checkout", "-f", "FETCH_HEAD"], 30)
                synced = ref or "default branch"
            elif ref.startswith("refs/"):
                # a full ref (e.g. a Gerrit change/patchset `refs/changes/68/85368/1`) can't be a
                # clone --branch; clone the repo, then fetch that exact ref and check it out
                _git(["clone", "--depth", "1", "--no-checkout", auth_url, str(dest)], 120)
                _git(["-C", str(dest), "fetch", "--depth", "1", auth_url, ref], 90)
                _git(["-C", str(dest), "checkout", "-f", "FETCH_HEAD"], 30)
                synced = ref
            else:
                try:
                    args = ["clone", "--depth", "1"] + (["--branch", ref] if ref else []) + [auth_url, str(dest)]
                    _git(args, 120)
                    synced = ref or "default branch"
                except subprocess.CalledProcessError:
                    # ref not a branch/tag, or wrong default-branch name → clone the repo's default branch
                    import shutil
                    shutil.rmtree(dest, ignore_errors=True)
                    _git(["clone", "--depth", "1", auth_url, str(dest)], 120)
                    synced = "default branch" + (f" (ref '{ref}' not found)" if ref else "")
            status, msg, ck = "ok", f"synced {synced}", str(dest)
        except subprocess.TimeoutExpired:
            status, msg, ck = "error", "timed out — no network access to the repo?", None
        except subprocess.CalledProcessError as e:
            status, msg, ck = "error", _scrub((e.stderr or e.stdout or str(e)).strip())[-500:], None
        except Exception as e:
            status, msg, ck = "error", _scrub(str(e))[-500:], None
        commit = _rev(dest) if status == "ok" else ""       # the loaded sha1 (provenance)
        repo.set_check_source_status(name, status, msg, ck, now, commit=commit, by=by)
        results.append({"name": name, "status": status, "message": msg})
    return results


def parse_md_test(text: str) -> tuple[dict, str]:
    """Split a Markdown manual-test artifact into (frontmatter dict, body). Frontmatter is the
    leading `---`-fenced YAML block; the body is the operator-facing Markdown."""
    import yaml
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            try:
                meta = yaml.safe_load(text[3:end]) or {}
            except Exception:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), text[end + 4:].lstrip("\n")
    return {}, text


def _md_model(rel_parts) -> str:
    """`atf_checks/<model>/…/<file>.md` → the <model> slug ("" for common). The drivers/actions
    a manual test needs come from its frontmatter, not the path."""
    return "" if len(rel_parts) < 2 or rel_parts[1] == "common" else rel_parts[1]


_MD_IDS: set[str] = set()             # ids registered from Markdown manual-test files (so reload can drop them)


def _discover_md() -> None:
    """Register every Markdown manual test under the current sources' atf_checks trees. Rebuilt
    on each call: drops the previously-registered md ids first so edits/removals take effect."""
    from atf.core.registry import REGISTRY, register_md_manual
    global _MD_IDS
    for cid in _MD_IDS:
        REGISTRY.pop(cid, None)
    _MD_IDS = set()
    for root in source_paths():
        checks = root / "atf_checks"
        if not checks.is_dir():
            continue
        for f in checks.rglob("*.md"):
            if "__pycache__" in f.parts:
                continue
            try:
                meta, body = parse_md_test(f.read_text())
            except Exception:
                continue
            cid = meta.get("id") or f.stem
            register_md_manual(cid, model=_md_model(f.relative_to(root).parts),
                               drivers=meta.get("drivers") or [], actions=meta.get("actions") or [],
                               severity=str(meta.get("severity", "medium")),
                               title=meta.get("title") or cid,
                               disruptive=bool(meta.get("disruptive")), body=body,
                               path=str(f.resolve()))
            _MD_IDS.add(cid)


_ADDED_PATHS: list[str] = []          # source paths we put on sys.path (so we can retract stale ones)


def discover() -> list[str]:
    """Put the CURRENT check sources on sys.path and import all modules under ``atf_checks``.
    Retracts any previously-added source path that is no longer current (so switching from dev
    siblings to synced checkouts, or dropping a repo, doesn't leave the old one importable).
    Idempotent. Returns the imported module names."""
    global _ADDED_PATHS
    current = [str(p.resolve()) for p in source_paths()]
    for old in _ADDED_PATHS:
        if old not in current and old in sys.path:
            sys.path.remove(old)
    for sp in current:
        if sp not in sys.path:
            sys.path.insert(0, sp)
    _ADDED_PATHS = current
    importlib.invalidate_caches()
    try:
        import atf_checks
    except ModuleNotFoundError:
        _discover_md()                 # no .py namespace, but still (re)build manual tests — this DROPS
        return []                      # stale ones from a removed source (finds none if no sources left)
    imported: list[str] = []
    for info in pkgutil.walk_packages(atf_checks.__path__, "atf_checks."):
        if not info.ispkg:
            importlib.import_module(info.name)
            imported.append(info.name)
    _discover_md()                     # Markdown manual tests are repo artifacts too, discovered here
    return imported


def reload_upstream() -> list[str]:
    """Hot-swap the discovered checks: drop every ``atf_checks.*`` registry entry AND purge the
    modules from ``sys.modules`` so they re-import fresh from the (possibly changed) sources, then
    re-discover (which also rebuilds the Markdown manual tests). Lets a runtime re-sync switch
    upstream checks with no server restart."""
    from atf.core.registry import REGISTRY
    for cid in [cid for cid, s in list(REGISTRY.items())
                if getattr(s.fn, "__module__", "").startswith("atf_checks")]:
        REGISTRY.pop(cid, None)
    for name in [n for n in list(sys.modules) if n == "atf_checks" or n.startswith("atf_checks.")]:
        del sys.modules[name]
    return discover()
