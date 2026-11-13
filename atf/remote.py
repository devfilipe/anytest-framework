"""`atf remote …` — the CLI as a client of a running atf server (its web API).

`atf remote login` asks for the password without echo and keeps only the session — server URL, user
and the token the server issued — in ``$ATF_CONFIG_DIR/session.json`` (default ``~/.config/atf``),
readable by the owner alone. Every other command authenticates with that token. Stdlib only.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


class RemoteError(Exception):
    """A failure to report to the operator as one line (no traceback); `status` is the HTTP status
    when the server answered."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _config_dir() -> Path:
    return Path(os.environ.get("ATF_CONFIG_DIR") or Path.home() / ".config" / "atf")


def _session_path() -> Path:
    return _config_dir() / "session.json"


def _save_session(data: dict) -> None:
    path = _session_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)                   # also when the file already existed with wider rights
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)


def _load_session() -> dict:
    try:
        return json.loads(_session_path().read_text())
    except (OSError, ValueError):
        raise RemoteError("no session: run `atf remote login --server URL --user NAME` first") from None


def _call(server: str, method: str, path: str, body=None, token: str = ""):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        with e:
            detail = e.read().decode("utf-8", "replace")[:300]
        raise RemoteError(f"{method} {path} → HTTP {e.code}: {detail}", e.code) from None
    except urllib.error.URLError as e:
        raise RemoteError(f"cannot reach {server}: {e.reason}") from None


def _api(method: str, path: str, body=None):
    s = _load_session()
    try:
        return _call(s["server"], method, path, body, s["token"])
    except RemoteError as e:
        if e.status == 401:           # tokens live in the server's memory: a restart drops them
            raise RemoteError(f"session expired on {s['server']}: run `atf remote login --server "
                              f"{s['server']} --user {s.get('user', 'NAME')}` again") from None
        raise


def login(server: str, user: str) -> None:
    password = getpass.getpass(f"password for {user} at {server}: ")
    r = _call(server, "POST", "/api/admin/login", {"user": user, "password": password})
    _save_session({"server": server.rstrip("/"), "user": user, "token": r["token"]})
    print(f"logged in to {server} as {user}")


def logout() -> None:
    """Forget the local session (the server drops the token itself on restart)."""
    try:
        _session_path().unlink()
    except FileNotFoundError:
        pass
    print("logged out")


def bench_list() -> None:
    for b in _api("GET", "/api/benches") or []:
        print(b["name"] if isinstance(b, dict) else b)


def bench_get(name: str) -> None:
    print(json.dumps(_api("GET", f"/api/benches/{urllib.request.quote(name)}"), indent=2, ensure_ascii=False))


def bench_put(name: str, file: str) -> None:
    import yaml                            # a core dependency; JSON is valid YAML, so either is read
    try:
        data = yaml.safe_load(Path(file).read_text())
    except (OSError, yaml.YAMLError) as e:
        raise RemoteError(f"cannot read {file}: {e}") from None
    if not isinstance(data, dict):
        raise RemoteError(f"{file} is not a bench (a mapping with agents/boards)")
    _api("PUT", f"/api/benches/{urllib.request.quote(name)}", data)
    print(f"bench {name} written")


def _pairs(items: list[str], what: str) -> dict:
    """`k=v` options as a dict; an all-digit value is a number (ports)."""
    out = {}
    for item in items or []:
        if "=" not in item:
            raise RemoteError(f"{what} {item!r}: expected key=value")
        k, v = item.split("=", 1)
        out[k.strip()] = int(v) if v.strip().isdigit() else v.strip()
    return out


def driver_set(bench: str, board: str, alias: str, props: list[str], restconf: list[str], no_restconf: bool) -> None:
    """Change one driver of one board of a bench (its props and its `restconf` block), keeping the rest
    of the bench as the server has it."""
    q = urllib.request.quote(bench)
    data = _api("GET", f"/api/benches/{q}")
    target = next((b for b in data.get("boards") or [] if b.get("name") == board), None)
    if target is None:
        raise RemoteError(f"bench {bench} has no board {board}")
    drv = target.setdefault("drivers", {}).setdefault(alias, {"driver_name": "ip"})
    drv.update(_pairs(props, "--prop"))
    if no_restconf:
        drv.pop("restconf", None)
    elif restconf:
        drv["restconf"] = {**(drv.get("restconf") or {}), **_pairs(restconf, "--restconf")}
    _api("PUT", f"/api/benches/{q}", {"agents": data.get("agents") or {}, "boards": data["boards"]})
    print(f"driver {alias} of {board} in bench {bench} written")


def secret_set(bench: str, ref: str, file: str) -> None:
    """Store one secret of a bench from a file (a PEM, a password file); the value is never printed."""
    try:
        value = Path(file).read_text()
    except OSError as e:
        raise RemoteError(f"cannot read {file}: {e.strerror}") from None
    _api("PUT", f"/api/benches/{urllib.request.quote(bench)}/secrets", {"secrets": {ref: value}})
    print(f"secret {ref} of bench {bench} written ({len(value)} characters)")


def add_parser(sub) -> None:
    rp = sub.add_parser("remote", help="talk to a running atf server (login, benches, secrets)")
    rs = rp.add_subparsers(dest="remote_cmd", required=True)
    lg = rs.add_parser("login", help="log in and keep the session token (asks for the password)")
    lg.add_argument("--server", required=True, help="atf server URL, e.g. http://192.0.2.10:8899")
    lg.add_argument("--user", required=True)
    rs.add_parser("logout", help="forget the local session")
    bp = rs.add_parser("bench", help="read or write a bench of the server's store")
    bs = bp.add_subparsers(dest="bench_cmd", required=True)
    bs.add_parser("list", help="the bench names")
    bg = bs.add_parser("get", help="print a bench as JSON")
    bg.add_argument("name")
    bw = bs.add_parser("put", help="write a bench from a YAML or JSON file (replaces it)")
    bw.add_argument("name")
    bw.add_argument("file")
    dp = rs.add_parser("driver", help="change one driver of a board in a bench")
    ds = dp.add_subparsers(dest="driver_cmd", required=True)
    dset = ds.add_parser("set", help="set props and the restconf (mTLS) block of a driver")
    dset.add_argument("bench")
    dset.add_argument("board")
    dset.add_argument("alias", help="the driver alias (mgmt, console, craft, …)")
    dset.add_argument("--prop", action="append", default=[], help="key=value of the driver (repeatable), e.g. ip=192.0.2.1")
    dset.add_argument("--restconf", action="append", default=[],
                      help="key=value of the restconf block (repeatable): scheme, port, role, cert_ref, key_ref, ca_ref")
    dset.add_argument("--no-restconf", action="store_true", help="remove the restconf block")
    sp = rs.add_parser("secret", help="write a bench secret on the server (encrypted at rest there)")
    ss = sp.add_subparsers(dest="secret_cmd", required=True)
    st = ss.add_parser("set", help="set one secret from a file; the value is never printed")
    st.add_argument("bench")
    st.add_argument("ref", help="the name a bench *_ref points to")
    st.add_argument("--file", required=True, help="file holding the value (e.g. a PEM)")


def run(args) -> int:
    try:
        if args.remote_cmd == "login":
            login(args.server, args.user)
        elif args.remote_cmd == "logout":
            logout()
        elif args.remote_cmd == "bench" and args.bench_cmd == "list":
            bench_list()
        elif args.remote_cmd == "bench" and args.bench_cmd == "get":
            bench_get(args.name)
        elif args.remote_cmd == "bench" and args.bench_cmd == "put":
            bench_put(args.name, args.file)
        elif args.remote_cmd == "driver" and args.driver_cmd == "set":
            driver_set(args.bench, args.board, args.alias, args.prop, args.restconf, args.no_restconf)
        elif args.remote_cmd == "secret" and args.secret_cmd == "set":
            secret_set(args.bench, args.ref, args.file)
        return 0
    except RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
