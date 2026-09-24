"""In-memory registry of dev/host agents connected to this server (Mode A: working-tree runs).

An agent runs on the developer's own machine and connects OUTBOUND (HTTP long-poll — NAT/proxy
friendly, no inbound SSH). It registers its local check-source working trees; on request it
uploads a tarball of them, which the server runs in an isolated, dev-tagged subprocess so the
developer's *uncommitted* code executes against the real bench without a git round-trip.
"""
from __future__ import annotations

import io
import os
import queue
import secrets
import tarfile
import tempfile
import threading
import time
from pathlib import Path


def _now() -> float:
    return time.time()


def work_root() -> str | None:
    """Base dir for the app's scratch dirs (uploaded trees, run outputs). Defaults to the system
    temp dir (fine for dev/local). In a hardened deployment `PrivateTmp=true` hides /tmp from the
    docker daemon, so mgmt `--mgmt-backend docker` mounts resolve to nothing; set $ATF_WORK to a
    host-visible, service-writable path (e.g. /var/lib/atf/work) so those bind mounts work."""
    w = os.environ.get("ATF_WORK") or None
    if w:
        os.makedirs(w, exist_ok=True)
    return w


class AgentSession:
    def __init__(self, sid: str, name: str, sources: list, vantages: dict, platform: str,
                 owner: str = "", proto: int = 0):
        self.id = sid
        self.name = name
        self.owner = owner               # the user whose enrollment token this agent registered with
        self.proto = proto               # agent build/command-surface version (0 = pre-versioning build)
        self.sources = sources          # [{name, repo, branch, dirty, head}]
        self.vantages = vantages         # free-form reachability facts {mgmt:bool, craft:bool, …}
        self.platform = platform
        self.last_seen = _now()
        self.cmds: queue.Queue = queue.Queue()
        self.uploads: dict = {}          # token -> {"event": Event, "dir": Path|None}
        self.inspects: dict = {}         # token -> {"event": Event, "data": list|None}
        self.catalogs: dict = {}         # token -> {"event": Event, "data": list|None}
        self.catalog: list = []          # last-known check catalog (from register / refresh) — LIVE
        self.loaded: dict = {}           # {path: sha} snapshot the app "loaded" (connect/Sync) — for app↔fs delta
        self.req_files: list = []        # raw requirements/*.yaml the agent provides (server parses)
        self.files: dict = {}            # token -> {"event": Event, "data": dict|None} (read/write file)
        self.ai: dict = {"on": False, "path": "", "claude": None, "unrestricted": True, "model": ""}   # AI state

    def snapshot(self) -> None:
        """Mark the current catalog as what the app has loaded (clears delta until the agent's files change)."""
        self.loaded = {c.get("path"): c.get("sha") for c in (self.catalog or [])}

    def touch(self) -> None:
        self.last_seen = _now()

    def has_pending(self) -> bool:
        """True while the agent still owes a result for an in-flight request (tree upload, inspect,
        catalog, file op, AI enable/run). A long job (a headless Wizard run can take minutes) blocks
        the agent's single poll loop, so it can't heartbeat — the hub must NOT reap it meanwhile, or
        the in-flight job is lost and the agent is forced to reconnect."""
        for store in (self.uploads, self.inspects, self.catalogs, self.files):
            for v in store.values():
                ev = v.get("event")
                if ev is not None and not ev.is_set():
                    return True
        return False

    def info(self) -> dict:
        return {"id": self.id, "name": self.name, "sources": self.sources,
                "vantages": self.vantages, "platform": self.platform, "owner": self.owner,
                "proto": self.proto, "ai": self.ai, "idle": round(_now() - self.last_seen, 1)}


class AgentHub:
    def __init__(self, ttl: float = 45.0, busy_ttl: float = 600.0):
        self.sessions: dict[str, AgentSession] = {}
        self.lock = threading.Lock()
        self.ttl = ttl
        # A session with an in-flight job blocks the agent's poll loop (no heartbeat), so grant it a
        # much longer grace than the idle TTL before reaping — long enough to outlast a headless AI
        # run — while still bounding a truly-dead agent that died mid-job.
        self.busy_ttl = busy_ttl

    def register(self, name: str, sources: list, vantages: dict, platform: str,
                 catalog: list | None = None, req_files: list | None = None, owner: str = "",
                 proto: int = 0) -> AgentSession:
        s = AgentSession(secrets.token_hex(8), name, sources or [], vantages or {}, platform, owner, proto)
        s.catalog = catalog or []
        s.req_files = req_files or []
        s.snapshot()                     # connecting loads the agent's current tree into the app
        with self.lock:
            self.sessions[s.id] = s
        return s

    def get(self, sid: str) -> AgentSession | None:
        with self.lock:
            return self.sessions.get(sid)

    def drop(self, sid: str) -> bool:
        """Forget an agent (disconnect). Its overlay — checks + requirement catalogs — is not
        persisted, so dropping the session removes everything it provided from the platform."""
        with self.lock:
            return self.sessions.pop(sid, None) is not None

    def alive(self) -> list[AgentSession]:
        """Live agents; forget any that missed the poll TTL. A session running a long job (which
        blocks its poll loop, so it can't heartbeat) is kept until the longer `busy_ttl` so an
        in-flight run/upload isn't reaped out from under itself."""
        now = _now()
        with self.lock:
            for k in list(self.sessions):
                s = self.sessions[k]
                grace = self.busy_ttl if s.has_pending() else self.ttl
                if now - s.last_seen > grace:
                    self.sessions.pop(k, None)
            return list(self.sessions.values())

    def request_tree(self, s: AgentSession) -> tuple[str, threading.Event]:
        tok = secrets.token_hex(8)
        ev = threading.Event()
        s.uploads[tok] = {"event": ev, "dir": None}
        s.cmds.put({"cmd": "push-tree", "token": tok})
        return tok, ev

    def receive_tree(self, s: AgentSession, tok: str, raw: bytes) -> Path:
        d = Path(tempfile.mkdtemp(prefix="atf-agent-tree-", dir=work_root()))
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tf:
            tf.extractall(d, filter="data")       # dev content, admin-gated; data filter blocks traversal
        up = s.uploads.get(tok)
        if up is not None:
            up["dir"] = d
            up["event"].set()
        return d

    def request_inspect(self, s: AgentSession) -> tuple[str, threading.Event]:
        tok = secrets.token_hex(8)
        ev = threading.Event()
        s.inspects[tok] = {"event": ev, "data": None}
        s.cmds.put({"cmd": "inspect", "token": tok})
        return tok, ev

    def receive_inspect(self, s: AgentSession, tok: str, data: list) -> None:
        it = s.inspects.get(tok)
        if it is not None:
            it["data"] = data
            it["event"].set()

    def request_catalog(self, s: AgentSession) -> tuple[str, threading.Event]:
        tok = secrets.token_hex(8)
        ev = threading.Event()
        s.catalogs[tok] = {"event": ev, "data": None}
        s.cmds.put({"cmd": "catalog", "token": tok})
        return tok, ev

    def request_file(self, s: AgentSession, op: str, source: str, path: str,
                     content: str | None = None) -> tuple[str, threading.Event]:
        tok = secrets.token_hex(8)
        ev = threading.Event()
        s.files[tok] = {"event": ev, "data": None}
        cmd = {"cmd": f"file-{op}", "token": tok, "source": source, "path": path}
        if content is not None:
            cmd["content"] = content
        s.cmds.put(cmd)
        return tok, ev

    def request_ai(self, s: AgentSession, path: str, pack_b64: str, server: str,
                   token: str) -> tuple[str, threading.Event]:
        """Turn AI on: the agent extracts the embedded resource pack to `path` and records the
        server URL + token so the pack's skills can reach the API. Reuses the `files` result store."""
        tok = secrets.token_hex(8)
        ev = threading.Event()
        s.files[tok] = {"event": ev, "data": None}
        s.cmds.put({"cmd": "ai-enable", "token": tok, "path": path, "pack": pack_b64,
                    "server": server, "server_token": token})
        return tok, ev

    def request_ai_run(self, s: AgentSession, path: str, prompt: str, resume: str = "",
                       unrestricted: bool = True, model: str = "") -> tuple[str, threading.Event]:
        """Run a headless Claude Code prompt on the agent (from the AI pack dir). `resume` continues
        a prior Wizard session; `unrestricted` picks the permission mode; `model` picks the Claude
        model. Reuses the files store."""
        tok = secrets.token_hex(8)
        ev = threading.Event()
        s.files[tok] = {"event": ev, "data": None}
        s.cmds.put({"cmd": "ai-run", "token": tok, "path": path, "prompt": prompt,
                    "resume": resume, "unrestricted": unrestricted, "model": model})
        return tok, ev

    def receive_file(self, s: AgentSession, tok: str, data: dict) -> None:
        it = s.files.get(tok)
        if it is not None:
            it["data"] = data
            it["event"].set()

    def receive_catalog(self, s: AgentSession, tok: str, data: list, req_files: list | None = None) -> None:
        s.catalog = data or []                          # refresh the cache
        if req_files is not None:
            s.req_files = req_files
        it = s.catalogs.get(tok)
        if it is not None:
            it["data"] = data
            it["event"].set()
