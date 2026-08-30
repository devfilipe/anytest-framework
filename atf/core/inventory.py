"""Load a bench inventory (benches/<name>.yaml) into typed objects. Each bench has its
own gitignored secrets file next to it — `benches/<name>.secrets.yaml` — resolved
automatically from the bench path; `*_ref` names in the inventory look up into it."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import os

import yaml


@dataclass
class Creds:
    user: str
    password: str = ""


@dataclass
class Agent:
    """A node that bridges a vector. Platform + connection only — NOT tied to a vector;
    the board's vector binding chooses which agent serves console/craft."""
    name: str
    platform: str = "linux"             # linux | windows
    host: str = ""
    ssh_user: str = ""
    ssh_password: str = ""
    ssh_key: str = ""                   # optional private-key path; empty ⇒ password or default keys
    raw: dict = field(default_factory=dict)


@dataclass
class Board:
    name: str
    model: str
    serial: str = ""
    creds: dict[str, Creds] = field(default_factory=dict)    # from the bench (role -> Creds)
    drivers: dict[str, dict] = field(default_factory=dict)   # alias -> {type, ...resolved props} (the ip/address lives here)
    actions: dict[str, dict] = field(default_factory=dict)   # label -> {agent, signals{on/off/status}}


@dataclass
class Bench:
    agents: dict[str, Agent] = field(default_factory=dict)
    boards: list[Board] = field(default_factory=list)


def _resolve_pw(spec: dict, secrets: dict) -> str:
    """Resolve a credential. Lenient: an unresolved *_ref yields "" (checks that
    actually need it fail clearly at auth time), so the skeleton runs without secrets."""
    if "password" in spec:
        return str(spec["password"])
    ref = spec.get("password_ref")
    if ref is None:
        return ""
    return str(secrets.get(ref, ""))


def default_secrets_path(bench_path: str) -> Path:
    """`benches/lab.yaml` -> `benches/lab.secrets.yaml` (its per-bench secrets)."""
    p = Path(bench_path)
    return p.with_name(p.stem + ".secrets.yaml")


def parse(data: dict, secrets: dict) -> Bench:
    """Build a Bench from a bench dict + resolved secrets. Shared by the YAML loader and
    the DB-backed store (which hands the same dict shape)."""
    data = data or {}
    secrets = secrets or {}
    agents = {}
    for name, a in (data.get("agents") or {}).items():
        ssh = a.get("ssh") or {}
        agents[name] = Agent(
            name=name, platform=a.get("platform", "linux"), host=a.get("host", ""),
            ssh_user=ssh.get("user", ""), ssh_password=_resolve_pw(ssh, secrets),
            ssh_key=os.path.expanduser(ssh.get("key", "") or ""), raw=a)

    boards = []
    for b in (data.get("boards") or []):
        creds = {
            role: Creds(user=c.get("user", role), password=_resolve_pw(c, secrets))
            for role, c in (b.get("creds") or {}).items()
        }
        boards.append(Board(
            name=b["name"], model=b.get("model", ""), serial=b.get("serial", ""),
            creds=creds, drivers=b.get("drivers") or {}, actions=b.get("actions") or {}))

    return Bench(agents=agents, boards=boards)


def load(bench_path: str, secrets_path: str | None = None) -> Bench:
    data = yaml.safe_load(Path(bench_path).read_text()) or {}
    sp = Path(secrets_path) if secrets_path else default_secrets_path(bench_path)
    secrets: dict = {}
    if sp.exists():
        secrets = yaml.safe_load(sp.read_text()) or {}
    return parse(data, secrets)
