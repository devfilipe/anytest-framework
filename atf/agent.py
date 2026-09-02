"""`atf agent` — the dev/host agent (Mode A).

Runs on the developer's machine, connects OUTBOUND to a atf server, registers its local
check-source working trees, and on request uploads a tarball of them so the server runs the
developer's *uncommitted* code against the bench. Stdlib only, so it installs anywhere.
"""
from __future__ import annotations

import io
import json
import os
import platform
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

_SKIP = {".git", "__pycache__", ".venv", ".ruff_cache", "reports", "node_modules"}

# Agent build/command-surface version. Bump when the set of poll commands the agent understands
# changes; the server surfaces it (Admin › Agents) so an outdated agent is visible at a glance.
AGENT_PROTO = 1


def _git(path: Path, *args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(path), *args],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _source_info(path: Path) -> dict:
    return {"name": path.name,
            "repo": _git(path, "rev-parse", "--show-toplevel") and path.name,
            "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD"),
            "head": _git(path, "rev-parse", "--short", "HEAD"),
            "dirty": bool(_git(path, "status", "--porcelain"))}


def _model_of(relpath: str) -> str:
    parts = relpath.replace("\\", "/").split("/")
    if len(parts) >= 2 and parts[0] == "atf_checks":
        return "" if parts[1] == "common" else parts[1]
    return ""


def _parse_md_frontmatter(text: str) -> dict:
    """Stdlib-only frontmatter parse (the agent ships no yaml). Handles `key: value`,
    `disruptive: true`, and inline lists `key: [a, b]` (drivers/actions/requirements)."""
    meta: dict = {}
    if not text.startswith("---"):
        return meta
    end = text.find("\n---", 3)
    if end == -1:
        return meta
    for line in text[3:end].splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip("'\"") for x in v[1:-1].split(",") if x.strip()]
        elif k == "disruptive":
            meta[k] = v.lower() in ("true", "1", "yes")
        else:
            meta[k] = v.strip("'\"")
    return meta


def _catalog(sources: list[Path]) -> list[dict]:
    """Extract each check's @register metadata by parsing the files with `ast` — NO import,
    so the zero-install agent (stdlib only, no atf package) can still advertise its checks."""
    import ast
    import hashlib
    out = []
    for p in sources:
        checks = p / "atf_checks"
        if not checks.is_dir():
            continue
        for f in checks.rglob("*.py"):
            if "__pycache__" in f.parts:
                continue
            try:
                raw = f.read_bytes()
                tree = ast.parse(raw)
            except Exception:
                continue
            sha = hashlib.sha1(raw).hexdigest()[:12]   # file-content signature → app↔filesystem delta
            model = _model_of(str(f.relative_to(p)))
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for dec in node.decorator_list:
                    fn = getattr(dec, "func", None)
                    name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                    if not isinstance(dec, ast.Call) or name != "register":
                        continue
                    kw = {}
                    for k in dec.keywords:
                        try:
                            kw[k.arg] = ast.literal_eval(k.value)   # str/list/tuple/bool literals
                        except Exception:
                            kw[k.arg] = None
                    if not kw.get("id"):
                        continue
                    drivers = sorted(kw.get("drivers") or [])
                    out.append({"id": kw["id"], "requirements": list(kw.get("requirements") or []),
                                "drivers": drivers, "actions": sorted(kw.get("actions") or []),
                                "mode": kw.get("mode") or "auto",
                                "model": kw["model"] if kw.get("model") is not None else model,
                                "disruptive": bool(kw.get("disruptive")),
                                "source": p.name, "path": str(f.relative_to(p)), "sha": sha})
        # Markdown manual tests: repo artifacts (atf_checks/<model>/manual/<id>.md), advertised like
        # code checks with mode=manual — model from the path, drivers/actions/… from frontmatter.
        for f in checks.rglob("*.md"):
            if "__pycache__" in f.parts:
                continue
            try:
                raw = f.read_bytes()
            except Exception:
                continue
            rel = str(f.relative_to(p))
            meta = _parse_md_frontmatter(raw.decode("utf-8", "replace"))
            drivers = sorted(meta.get("drivers") or [])
            out.append({"id": meta.get("id") or f.stem, "requirements": list(meta.get("requirements") or []),
                        "drivers": drivers, "actions": sorted(meta.get("actions") or []),
                        "mode": "manual", "model": _model_of(rel),
                        "disruptive": bool(meta.get("disruptive")),
                        "source": p.name, "path": rel, "sha": hashlib.sha1(raw).hexdigest()[:12]})
    return out


def _ai_enable(path: str, pack_b64: str, server: str, server_token: str,
               aid: str = "", sources: list | None = None) -> dict:
    """Extract the resource pack (CLAUDE.md + skills + MCP server) to the user's env and drop a
    `.atf-ai.env` with the server URL/token, this agent's id, and the LOCAL SOURCE REPO PATHS — so
    Claude Code can reach the API (skills / MCP) AND edit the actual test files. Detects the `claude`
    CLI. Everything runs in the developer's environment (AI is local, not on the server)."""
    import base64
    import io
    import json
    import shutil
    import tarfile
    try:
        dest = Path(os.path.expanduser(path or "~/atf-ai")).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(base64.b64decode(pack_b64.encode())), mode="r:gz") as tf:
            tf.extractall(dest)
            deployed = sorted((m.name[2:] if m.name.startswith("./") else m.name)          # what WE shipped
                              for m in tf.getmembers() if m.isfile())
        env = [f"ATF_SERVER={server}", f"ATF_TOKEN={server_token}", f"ATF_AID={aid}",
               f"ATF_SOURCES={os.pathsep.join(sources or [])}"]
        (dest / ".atf-ai.env").write_text("\n".join(env) + "\n")
        # manifest of framework-deployed files, so disconnect removes exactly these (never user files)
        manifest = deployed + [".atf-ai.env"]
        (dest / ".atf-ai-manifest.json").write_text(json.dumps({"files": manifest}))
        return {"ok": True, "path": str(dest), "claude": bool(shutil.which("claude")),
                "sources": sources or [], "files": manifest}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _ai_disable(path: str) -> dict:
    """Remove the resource pack this framework deployed (from its manifest) and prune the dirs/pyc it
    left behind — WITHOUT touching any file the user put in the same directory."""
    import json
    import shutil
    try:
        dest = Path(os.path.expanduser(path or "~/atf-ai")).resolve()
        if not dest.is_dir():
            return {"ok": True, "removed": []}
        mf = dest / ".atf-ai-manifest.json"
        files = []
        if mf.is_file():
            try:
                files = json.loads(mf.read_text()).get("files", [])
            except Exception:
                files = []
        files = list(dict.fromkeys(files + [".atf-ai.env", ".atf-ai-manifest.json"]))
        removed = []
        for rel in files:
            p = dest / rel
            if p.is_file():
                p.unlink()
                removed.append(rel)
        for pc in list(dest.rglob("__pycache__")):        # bytecode the MCP left when it ran
            shutil.rmtree(pc, ignore_errors=True)
        # prune every now-empty dir the pack created (all ancestors up to dest, deepest first)
        dirs = set()
        for rel in files:
            p = (dest / rel).parent
            while p != dest and dest in p.parents:
                dirs.add(p)
                p = p.parent
        for d in sorted(dirs, key=lambda x: -len(str(x))):
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        if dest.is_dir() and not any(dest.iterdir()):
            dest.rmdir()
        return {"ok": True, "removed": removed}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


_MCP_TOOLS = ("mcp__atf__atf_catalog,mcp__atf__atf_suites,mcp__atf__atf_scaffold,"
              "mcp__atf__atf_run,mcp__atf__atf_report,mcp__atf__atf_api,Read,Edit,Write")


def _claude(prompt: str, cwd: str | None = None, env: dict | None = None, add_dirs: list | None = None,
            resume: str | None = None, unrestricted: bool = True, model: str = "", timeout: int = 300) -> dict:
    """Headless Claude Code call in the developer's env. `--output-format json` gives us the
    session_id (so the Wizard continues the conversation via `resume`) and the result text.
    - unrestricted (default): `--dangerously-skip-permissions` — no prompts, full access (Bash etc).
    - restricted: `--permission-mode acceptEdits` + `--allowedTools` limited to the atf MCP tools +
      Read/Edit/Write (no shell). May need the workspace trusted once for tool allows to apply.
    `model` (alias like opus/sonnet/haiku, or a full id) → `--model`; empty = Claude Code's default.
    `add_dirs` lets it reach the source repos where the test files live."""
    cmd = ["claude", "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    if unrestricted:
        cmd += ["--dangerously-skip-permissions"]
    else:
        cmd += ["--permission-mode", "acceptEdits", "--allowedTools", _MCP_TOOLS]
    if resume:
        cmd += ["--resume", resume]
    for d in (add_dirs or []):
        cmd += ["--add-dir", d]
    try:
        r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return {"ok": False, "error": (r.stderr or r.stdout or "claude failed").strip()[:2000]}
        try:
            j = json.loads(r.stdout)
            u = j.get("usage") or {}
            return {"ok": not j.get("is_error"), "out": j.get("result", ""),
                    "session": j.get("session_id", ""), "err": r.stderr.strip()[:1000],
                    "meta": {"cost": j.get("total_cost_usd"), "dur_ms": j.get("duration_ms"),
                             "turns": j.get("num_turns"), "model": j.get("model") or model,
                             "in": u.get("input_tokens"), "out": u.get("output_tokens")}}
        except Exception:
            return {"ok": True, "out": r.stdout, "err": r.stderr.strip()[:1000]}
    except FileNotFoundError:
        return {"ok": False, "error": "claude CLI not found — install Claude Code on this machine"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"claude timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


def _ai_run(path: str, prompt: str, resume: str | None = None, unrestricted: bool = True,
            model: str = "") -> dict:
    """Run a headless Claude Code prompt from the AI pack dir, with the `.atf-ai.env` config in the
    environment and the source repos added so it can edit the real test files. `resume` continues a
    prior Wizard session; `unrestricted` picks the permission mode; `model` picks the Claude model."""
    if not (prompt or "").strip():
        return {"ok": False, "error": "empty prompt"}
    d = Path(os.path.expanduser(path or "~/atf-ai")).resolve()
    envf = {}
    ef = d / ".atf-ai.env"
    if ef.is_file():
        for line in ef.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                envf[k.strip()] = v.strip()
    sources = [p for p in envf.get("ATF_SOURCES", "").split(os.pathsep) if p]
    return _claude(prompt, cwd=str(d), env={**os.environ, **envf}, add_dirs=sources,
                   resume=resume, unrestricted=unrestricted, model=model)


def _resolve_file(sources, source_name, relpath):
    """Locate a check file the server asked for, sandboxed to a source's atf_checks tree."""
    for p in sources:
        if p.name == source_name:
            f = (p / relpath).resolve()
            base = (p / "atf_checks").resolve()
            if str(f).startswith(str(base) + "/") and f.suffix in (".py", ".md"):
                return f
    return None


def _requirement_files(sources: list[Path]) -> list[dict]:
    """Raw requirements/*.yaml from each source. The agent is stdlib-only (no yaml), so it ships
    the text and the SERVER parses it — an agent provides Requirements as well as Checks."""
    out = []
    for p in sources:
        d = p / "requirements"
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.yaml")):
            try:
                out.append({"name": f.name, "source": p.name, "yaml": f.read_text()[:200000]})
            except Exception:
                pass
    return out


def _inspect(sources: list[Path]) -> list[dict]:
    """Per source: git state + uncommitted diff + the check-file tree — so a reviewer on the
    server can see exactly what (possibly uncommitted) code a run would execute."""
    out = []
    for p in sources:
        checks = p / "atf_checks"
        files = sorted(str(f.relative_to(p)) for f in checks.rglob("*.py")
                       if "__pycache__" not in f.parts) if checks.is_dir() else []
        status = _git(p, "status", "--porcelain", "-uall")   # -uall: list untracked files, not dirs
        diff = _git(p, "diff", "HEAD")                  # tracked (committed) changes
        # git diff HEAD misses untracked files — surface new check .py as additions
        for line in status.splitlines():
            if line.startswith("??"):
                f = line[3:].strip().strip('"')
                if f.endswith(".py") and "atf_checks" in f and len(diff) < 180000:
                    diff += "\n" + _git(p, "diff", "--no-index", "--", "/dev/null", f)
        out.append({"name": p.name,
                    "branch": _git(p, "rev-parse", "--abbrev-ref", "HEAD"),
                    "head": _git(p, "rev-parse", "--short", "HEAD"),
                    "status": status, "diff": diff[:200000], "files": files})
    return out


def _tar(sources: list[Path]) -> bytes:
    """Pack each source dir as tree/<name>/… (skipping vcs/caches/secrets)."""
    buf = io.BytesIO()

    def _filter(ti: tarfile.TarInfo):
        parts = set(Path(ti.name).parts)
        if parts & _SKIP or ti.name.endswith(".secrets.yaml") or ti.name.endswith(".pyc"):
            return None
        return ti

    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for src in sources:
            tf.add(str(src), arcname=src.name, filter=_filter)
    return buf.getvalue()


def _post(url: str, body: dict, timeout: float = 30) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _get(url: str, timeout: float = 35) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _post_raw(url: str, raw: bytes, timeout: float = 60) -> None:
    req = urllib.request.Request(url, data=raw, method="POST",
                                 headers={"Content-Type": "application/gzip"})
    urllib.request.urlopen(req, timeout=timeout).read()


def run(server: str, token: str, sources: list[str], name: str) -> int:
    server = server.rstrip("/")
    paths = [Path(s).resolve() for s in sources]
    for p in paths:
        if not (p / "atf_checks").is_dir():
            print(f"warning: {p} has no atf_checks/ — not a check-source repo")
    src_info = [_source_info(p) for p in paths]
    reg = {"name": name or platform.node(), "token": token, "sources": src_info,
           "vantages": {}, "platform": platform.system(), "proto": AGENT_PROTO,
           "catalog": _catalog(paths), "req_files": _requirement_files(paths)}

    aid = None
    ai_path = None                                # set when an AI pack is deployed; removed on stop
    while True:
        try:
            if aid is None:
                # Re-scan on every (re)register so the catalog reflects the CURRENT files — a server
                # restart must not re-register with the stale startup catalog, or tests created during
                # the session would be wrongly flagged 'stale' (their file isn't in the app snapshot).
                src_info = [_source_info(p) for p in paths]
                reg["sources"], reg["catalog"], reg["req_files"] = (
                    src_info, _catalog(paths), _requirement_files(paths))
                aid = _post(f"{server}/api/agents/register", reg)["id"]
                dirty = ", ".join(f"{s['name']}{'*' if s['dirty'] else ''}" for s in src_info)
                print(f"registered as {aid}  ·  sources: {dirty}  ·  polling {server}")
            cmd = _get(f"{server}/api/agents/{aid}/poll")
            if cmd.get("cmd") == "stop":
                if ai_path:                          # tidy up: remove what the framework deployed here
                    r = _ai_disable(ai_path)
                    print(f"  🧹 removed AI pack from {ai_path}" if r.get("ok")
                          else f"  AI pack cleanup failed: {r.get('error')}")
                print("disconnected by the server; exiting")
                return 0
            if cmd.get("cmd") == "push-tree":
                print("  → server requested working tree; uploading…")
                _post_raw(f"{server}/api/agents/{aid}/tree?token={cmd['token']}", _tar(paths))
                print("  ✓ tree uploaded")
            elif cmd.get("cmd") == "inspect":
                _post(f"{server}/api/agents/{aid}/inspect?token={cmd['token']}",
                      {"sources": _inspect(paths)})
                print("  ✓ sent tree/diff to server")
            elif cmd.get("cmd") == "catalog":
                _post(f"{server}/api/agents/{aid}/catalog?token={cmd['token']}",
                      {"catalog": _catalog(paths), "req_files": _requirement_files(paths)})
            elif cmd.get("cmd") == "file-read":
                f = _resolve_file(paths, cmd.get("source"), cmd.get("path"))
                _post(f"{server}/api/agents/{aid}/file?token={cmd['token']}",
                      {"ok": bool(f and f.is_file()), "content": f.read_text() if f and f.is_file() else ""})
            elif cmd.get("cmd") == "file-write":
                f = _resolve_file(paths, cmd.get("source"), cmd.get("path"))
                res = {"ok": False, "error": "path not found"}
                if f:
                    try:
                        content = cmd.get("content", "")
                        if f.suffix == ".py":
                            compile(content, str(f), "exec")   # never write un-parseable code
                        f.parent.mkdir(parents=True, exist_ok=True)   # allow creating a new .md
                        f.write_text(content)
                        res = {"ok": True}
                    except SyntaxError as e:
                        res = {"ok": False, "error": f"SyntaxError: {e.msg} (line {e.lineno})"}
                    except Exception as e:
                        res = {"ok": False, "error": str(e)}
                _post(f"{server}/api/agents/{aid}/file?token={cmd['token']}", res)
                if res.get("ok"):
                    print(f"  ✎ server edited {cmd.get('path')}")
            elif cmd.get("cmd") == "ai-enable":
                res = _ai_enable(cmd.get("path", ""), cmd.get("pack", ""),
                                 cmd.get("server") or server, cmd.get("server_token", ""),
                                 aid=aid, sources=[str(p) for p in paths])
                _post(f"{server}/api/agents/{aid}/file?token={cmd['token']}", res)
                if res.get("ok"):
                    ai_path = res.get("path")
                    print(f"  🤖 AI pack → {res.get('path')} (claude CLI: {res.get('claude')})")
            elif cmd.get("cmd") == "ai-run":
                # A headless Wizard run can take minutes. Run it in a BACKGROUND THREAD so this poll
                # loop keeps heartbeating (otherwise the server's idle TTL reaps our session mid-run,
                # the job is lost and we're forced to reconnect). The thread posts the result itself.
                print(f"  🤖 running claude: {cmd.get('prompt','')[:70]}…")
                def _run_ai(c=cmd, _aid=aid):
                    res = _ai_run(c.get("path", ""), c.get("prompt", ""), resume=c.get("resume"),
                                  unrestricted=c.get("unrestricted", True), model=c.get("model", ""))
                    try:
                        _post(f"{server}/api/agents/{_aid}/file?token={c['token']}", res, timeout=60)
                    except Exception as e:  # noqa: BLE001 - the run finished; just log a delivery failure
                        print(f"  ⚠ couldn't deliver claude result: {e}")
                threading.Thread(target=_run_ai, daemon=True).start()
            elif cmd.get("cmd") not in (None, "noop"):
                # An unknown command means the server speaks a newer protocol than this agent build.
                # Confirm the failure right away (if it carries a result token) so the server returns a
                # clear error instead of hanging until timeout, and tell the operator to update.
                unknown = cmd.get("cmd")
                print(f"  ⚠ unknown command '{unknown}' — update this agent: curl {server}/agent.py")
                if cmd.get("token"):
                    _post(f"{server}/api/agents/{aid}/file?token={cmd['token']}",
                          {"ok": False, "error": f"agent too old for '{unknown}' — reconnect to update"})
        except urllib.error.HTTPError as e:
            if e.code == 404:                     # server forgot us (restart/TTL) — re-register
                aid = None
                time.sleep(2)
            else:
                print(f"  http {e.code}; retrying")
                time.sleep(3)
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            time.sleep(3)                         # server down / poll timeout — reconnect
        except KeyboardInterrupt:
            print("\nagent stopped")
            return 0


def _main(argv=None) -> int:
    """Standalone entry — so the file runs as `python3 atf-agent.py …` with no install
    (it is stdlib-only and imports nothing else from atf). Also backs `atf agent`."""
    import argparse
    ap = argparse.ArgumentParser(prog="atf-agent", description="atf dev/host agent (Mode A)")
    ap.add_argument("--server", required=True, help="atf server URL, e.g. http://192.0.2.10:8899")
    ap.add_argument("--token", required=True, help="enrollment token (Admin › Agents)")
    ap.add_argument("--source", action="append", default=[],
                    help="local check-source repo (repeatable)")
    ap.add_argument("--name", default="", help="agent name (default: hostname)")
    a = ap.parse_args(argv)
    if not a.source:
        ap.error("pass at least one --source <repo dir>")
    return run(a.server, a.token, a.source, a.name)


if __name__ == "__main__":
    raise SystemExit(_main())
