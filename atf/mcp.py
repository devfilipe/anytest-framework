"""`atf mcp` — an MCP server (JSON-RPC 2.0 over stdio, newline-delimited) for a running atf server.

Everything the server's API does is a tool: one tool per API area (``atf_api_benches``,
``atf_api_inventory``, …) whose ``op`` is the operation (``"GET /api/benches/{name}"``), built at
start-up from the server's own ``/api/openapi.json`` so it follows the API as it changes. The guided
flows of the agent's AI pack (run, report, map, test plan, scaffold, …) are offered alongside.

It authenticates with the session of ``atf remote login``; the password never passes through it.
Secrets go in by file path only (the server never returns them in clear), operations that delete or
replace things need ``confirm: true``, and no response carries a token.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
from pathlib import Path

from atf import remote

# routes that are not tools: the session itself (`atf remote login` owns it), the agent wire
# protocol (an agent speaks it, not a person) and the SSE stream (not request/response)
NOT_TOOLS = {("POST", "/api/admin/login"), ("POST", "/api/agents/register"), ("GET", "/api/agents/{aid}/poll"),
             ("POST", "/api/agents/{aid}/tree"), ("POST", "/api/agents/{aid}/inspect"),
             ("POST", "/api/agents/{aid}/catalog"), ("POST", "/api/agents/{aid}/file"),
             ("GET", "/api/run/stream")}
_PARAM = re.compile(r"{([^}]+)}")


def _area(path: str) -> str:
    return path.split("/")[2] if path.count("/") >= 2 else "root"


def operations(spec: dict) -> dict[str, list[dict]]:
    """{area: [{op, method, path, summary, path_params, query_params}]} of every tool operation."""
    areas: dict[str, list[dict]] = {}
    for path, item in sorted(spec.get("paths", {}).items()):
        for method, op in item.items():
            if method.upper() not in ("GET", "POST", "PUT", "DELETE", "PATCH") or (method.upper(), path) in NOT_TOOLS:
                continue
            params = op.get("parameters", [])
            areas.setdefault(_area(path), []).append({
                "op": f"{method.upper()} {path}", "method": method.upper(), "path": path,
                "summary": op.get("summary", ""),
                "path_params": _PARAM.findall(path),
                "query_params": [p["name"] for p in params if p.get("in") == "query"],
            })
    return areas


def _tool(area: str, ops: list[dict]) -> dict:
    lines = []
    for o in ops:
        args = [f"{{{p}}}" for p in o["path_params"]] + [f"?{q}" for q in o["query_params"]]
        lines.append(f"- {o['op']}" + (f" — {o['summary']}" if o["summary"] else "")
                     + (f" [params: {', '.join(args)}]" if args else ""))
    return {
        "name": f"atf_api_{area.replace('-', '_')}",
        "description": f"atf API, area '{area}'. Pick the operation in `op`; path and query parameters go in "
                       "`params`, a JSON body in `body`. Operations that delete or replace need `confirm: true`."
                       + (" Secrets are given as {ref: file path}; the file content is sent, never echoed."
                          if area == "benches" else "") + "\n" + "\n".join(lines),
        "inputSchema": {"type": "object", "required": ["op"], "properties": {
            "op": {"type": "string", "enum": [o["op"] for o in ops]},
            "params": {"type": "object", "description": "path and query parameters by name"},
            "body": {"type": "object", "description": "JSON body (POST/PUT/PATCH)"},
            "confirm": {"type": "boolean", "description": "true to run an operation that deletes or replaces"}}},
        "annotations": {"readOnlyHint": all(o["method"] == "GET" for o in ops),
                        "destructiveHint": any(_destructive(o) for o in ops)},
    }


def _destructive(o: dict) -> bool:
    """Deletes, restores a snapshot, rotates a token or changes users/passwords."""
    path = o["path"]
    return (o["method"] == "DELETE" or "restore" in path or path.startswith("/api/users")
            and o["method"] != "GET" or path in ("/api/admin/password",)
            or (o["method"] == "POST" and path == "/api/agents/token"))


def _redact(value):
    """No token in a response: any key naming one is masked, at any depth."""
    if isinstance(value, dict):
        return {k: ("***" if "token" in k.lower() and isinstance(v, str) else _redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _flows() -> dict:
    """The guided tools of the agent's AI pack, calling the API through the `atf remote` session
    (instead of the pack's ATF_TOKEN). Its untyped `atf_api` is left out: the area tools cover it,
    with the confirm and secret rules."""
    from atf.agent_pack import atf_mcp as pack
    pack._api = lambda method, path, body=None: json.dumps(remote._api(method, path, body))
    return {schema["name"]: (schema, handler) for schema, handler in pack.TOOLS if schema["name"] != "atf_api"}


class Server:
    def __init__(self):
        self.session = remote._load_session()
        spec = remote._call(self.session["server"], "GET", "/api/openapi.json")
        self.ops = {o["op"]: o for ops in operations(spec).values() for o in ops}
        self.tools = [_tool(a, ops) for a, ops in operations(spec).items()]
        self.by_tool = {t["name"]: set(t["inputSchema"]["properties"]["op"]["enum"]) for t in self.tools}
        self.flows = _flows()
        self.tools += [schema for schema, _ in self.flows.values()]

    def call_flow(self, name: str, a: dict) -> str:
        text = self.flows[name][1](a)
        try:
            return json.dumps(_redact(json.loads(text)), indent=2, ensure_ascii=False)
        except ValueError:
            return text

    def call_api(self, tool: str, a: dict) -> str:
        op = self.ops.get(a.get("op", ""))
        if op is None or op["op"] not in self.by_tool[tool]:
            raise remote.RemoteError(f"{tool} has no operation {a.get('op')!r}")
        if _destructive(op) and a.get("confirm") is not True:
            raise remote.RemoteError(f"{op['op']} deletes or replaces: call again with confirm: true")
        params = dict(a.get("params") or {})
        path = op["path"]
        for name in op["path_params"]:
            if name not in params:
                raise remote.RemoteError(f"{op['op']} needs params.{name}")
            path = path.replace(f"{{{name}}}", urllib.parse.quote(str(params.pop(name)), safe=""))
        if params:
            path += "?" + urllib.parse.urlencode(params, doseq=True)
        body = a.get("body")
        if isinstance(body, dict) and isinstance(body.get("secrets"), dict):
            body = {**body, "secrets": self._secrets_from_files(body["secrets"])}
        result = remote._api(op["method"], path, body if op["method"] in ("POST", "PUT", "PATCH") else None)
        return json.dumps(_redact(result), indent=2, ensure_ascii=False)

    @staticmethod
    def _secrets_from_files(secrets: dict) -> dict:
        out = {}
        for ref, file in secrets.items():
            p = Path(str(file)).expanduser()
            if not p.is_file():
                raise remote.RemoteError(f"secret {ref}: give the path of a file holding it, not the value")
            out[ref] = p.read_text()
        return out

    def handle(self, msg: dict):
        method, p = msg.get("method"), msg.get("params") or {}
        if method == "initialize":
            return {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "atf", "version": "1.0.0"}}
        if method == "tools/list":
            return {"tools": self.tools}
        if method == "tools/call":
            name, args = p.get("name"), p.get("arguments") or {}
            try:
                if name in self.flows:
                    text = self.call_flow(name, args)
                elif name in self.by_tool:
                    text = self.call_api(name, args)
                else:
                    raise remote.RemoteError(f"unknown tool {name!r}")
                return {"content": [{"type": "text", "text": text}], "isError": False}
            except remote.RemoteError as e:
                return {"content": [{"type": "text", "text": str(e)}], "isError": True}
            except (KeyError, TypeError) as e:     # a guided flow called without a required argument
                return {"content": [{"type": "text", "text": f"missing or wrong argument: {e}"}], "isError": True}
        return {}


def run() -> int:
    try:
        server = Server()
    except remote.RemoteError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") is None:          # a notification: no response
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": server.handle(msg)}) + "\n")
        sys.stdout.flush()
    return 0
