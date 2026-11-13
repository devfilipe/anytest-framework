"""RESTCONF reads through an `ip` driver: the bench wires the driver with a `restconf` block whose
client-certificate material comes from the bench secrets by reference, and a check reads JSON with
`ctx.<alias>.restconf_get(path)`. The server here is a real local HTTPS server that demands a client
certificate (mutual TLS), with a throwaway CA generated per session."""
from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI")


def _openssl(*args, cwd):
    subprocess.run(["openssl", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="session")
def pki(tmp_path_factory):
    """A CA, a server certificate for 127.0.0.1 and a client certificate, as PEM text."""
    d = tmp_path_factory.mktemp("pki")
    _openssl("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
             "-keyout", "ca.key", "-out", "ca.pem", "-days", "1", "-subj", "/CN=test-ca",
             "-addext", "basicConstraints=critical,CA:TRUE",
             "-addext", "keyUsage=critical,keyCertSign,cRLSign", cwd=d)   # strict X.509 (Python ≥ 3.13)
    for name, ext in (("server", "subjectAltName=IP:127.0.0.1"), ("client", "extendedKeyUsage=clientAuth")):
        _openssl("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes",
                 "-keyout", f"{name}.key", "-out", f"{name}.csr", "-subj", f"/CN={name}-1", cwd=d)
        (d / f"{name}.ext").write_text(ext + "\n")
        _openssl("x509", "-req", "-in", f"{name}.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
                 "-CAcreateserial", "-out", f"{name}.pem", "-days", "1", "-extfile", f"{name}.ext", cwd=d)
    return {n: (d / n).read_text() for n in ("ca.pem", "server.pem", "server.key", "client.pem", "client.key")} | {
        "dir": d}


class _Handler(BaseHTTPRequestHandler):
    routes: dict = {}

    def do_GET(self):  # noqa: N802 — http.server API
        status, body = self.routes.get(self.path, (404, {"errors": "not found"}))
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/yang-data+json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def mtls_server(pki):
    """An HTTPS server on 127.0.0.1 that requires a client certificate signed by the test CA."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki["dir"] / "server.pem", pki["dir"] / "server.key")
    ctx.load_verify_locations(pki["dir"] / "ca.pem")
    ctx.verify_mode = ssl.CERT_REQUIRED
    handler = type("H", (_Handler,), {"routes": {}})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], handler.routes
    srv.shutdown()
    srv.server_close()


def _mgmt_channel(port, restconf, secrets):
    """The mgmt channel exactly as a run builds it from a bench dict and its secrets."""
    from atf.access.channels.ip import IpChannel
    from atf.core.inventory import parse
    bench = parse({"boards": [{"name": "board-1", "creds": {"monitor": {"user": "monitor"}},
                               "drivers": {"mgmt": {"type": "ip", "ip": "127.0.0.1",
                                                    "restconf": {"port": port, **restconf}}}}]}, secrets)
    board = bench.boards[0]
    return IpChannel(board.drivers["mgmt"], None, board.creds, tls=board.tls.get("mgmt"))


def test_restconf_get_reads_json_over_mutual_tls(pki, mtls_server):
    port, routes = mtls_server
    routes["/restconf/data/acme:system/state"] = (200, {"acme:state": {"version": "1.0"}})
    ch = _mgmt_channel(port, {"scheme": "https", "role": "monitor",
                              "cert_ref": "c", "key_ref": "k", "ca_ref": "ca"},
                       {"c": pki["client.pem"], "k": pki["client.key"], "ca": pki["ca.pem"]})

    status, body = ch.restconf_get("acme:system/state")

    assert status == 200
    assert body == {"acme:state": {"version": "1.0"}}


def _mtls(pki, port):
    return _mgmt_channel(port, {"scheme": "https", "role": "monitor", "cert_ref": "c", "key_ref": "k", "ca_ref": "ca"},
                         {"c": pki["client.pem"], "k": pki["client.key"], "ca": pki["ca.pem"]})


def test_restconf_get_returns_an_http_error_status_with_its_body(pki, mtls_server):
    port, _routes = mtls_server

    status, body = _mtls(pki, port).restconf_get("acme:missing")

    assert status == 404
    assert body == {"errors": "not found"}


def test_restconf_get_over_http_authenticates_with_the_role_credentials():
    seen = {}

    class H(_Handler):
        def do_GET(self):  # noqa: N802
            seen["auth"] = self.headers.get("Authorization")
            super().do_GET()

    H.routes = {"/restconf/data/acme:system/state": (200, {"ok": True})}
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        from atf.access.channels.ip import IpChannel
        from atf.core.inventory import parse
        board = parse({"boards": [{"name": "board-1",
                                   "creds": {"monitor": {"user": "monitor", "password_ref": "pw"}},
                                   "drivers": {"mgmt": {"type": "ip", "ip": "127.0.0.1",
                                                        "restconf": {"scheme": "http", "role": "monitor",
                                                                     "port": srv.server_address[1]}}}}]},
                      {"pw": "s3cret"}).boards[0]
        ch = IpChannel(board.drivers["mgmt"], None, board.creds, tls=board.tls.get("mgmt"))

        status, body = ch.restconf_get("acme:system/state")
    finally:
        srv.shutdown()
        srv.server_close()

    import base64
    assert (status, body) == (200, {"ok": True})
    assert seen["auth"] == "Basic " + base64.b64encode(b"monitor:s3cret").decode()


def test_restconf_get_without_a_client_certificate_fails_the_transport(pki, mtls_server):
    port, routes = mtls_server
    routes["/restconf/data/acme:system/state"] = (200, {"ok": True})
    ch = _mgmt_channel(port, {"scheme": "https", "ca_ref": "ca"}, {"ca": pki["ca.pem"]})

    with pytest.raises(OSError):           # URLError/SSLError: the server refused the handshake
        ch.restconf_get("acme:system/state")


def test_a_mgmt_check_reads_restconf_through_the_worker_with_the_bench_certificate(pki, mtls_server, tmp_path):
    """The certificate material reaches the mgmt worker (container or local) with the request,
    so a check run there authenticates the same way."""
    from atf.access.mgmt.dispatch import dispatch
    from atf.core.inventory import parse
    from atf.core.model import Result, Verdict
    from atf.core.registry import REGISTRY, register

    port, routes = mtls_server
    routes["/restconf/data/acme:system/state"] = (200, {"acme:state": {"version": "2.0"}})

    @register(id="test-restconf-read", drivers=("mgmt",), title="reads one RESTCONF path")
    def _read(ctx):
        status, body = ctx.mgmt.restconf_get("acme:system/state")
        return Result(Verdict.PASS, metrics={"status": status, "body": body})

    board = parse({"boards": [{"name": "board-1", "drivers": {"mgmt": {
        "type": "ip", "ip": "127.0.0.1",
        "restconf": {"port": port, "cert_ref": "c", "key_ref": "k", "ca_ref": "ca"}}}}]},
        {"c": pki["client.pem"], "k": pki["client.key"], "ca": pki["ca.pem"]}).boards[0]
    try:
        [result] = dispatch(board, [REGISTRY["test-restconf-read"]], tmp_path, backend="local")
    finally:
        REGISTRY.pop("test-restconf-read", None)

    assert result["verdict"] == "pass", result["detail"]
    assert result["metrics"] == {"status": 200, "body": {"acme:state": {"version": "2.0"}}}


# --- writes: restconf_request(method, path, body=None, auth=None) ---

class _Recorder(BaseHTTPRequestHandler):
    """Answers every method from `routes[(method, path)]` and records what it received."""
    routes: dict = {}
    seen: list = []

    def _any(self):
        n = int(self.headers.get("Content-Length") or 0)
        cert = self.connection.getpeercert() if hasattr(self.connection, "getpeercert") else None
        self.seen.append({"method": self.command, "path": self.path, "body": self.rfile.read(n).decode(),
                          "type": self.headers.get("Content-Type"), "accept": self.headers.get("Accept"),
                          "auth": self.headers.get("Authorization"), "cert": bool(cert)})
        status, body = self.routes.get((self.command, self.path), (404, {"errors": "not found"}))
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        if data:
            self.send_header("Content-Type", "application/yang-data+json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _any  # noqa: N815 — http.server API

    def log_message(self, *args):
        pass


def _recorder(ctx=None):
    handler = type("R", (_Recorder,), {"routes": {}, "seen": []})
    srv = HTTPServer(("127.0.0.1", 0), handler)
    if ctx is not None:
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, handler


@pytest.fixture
def tls_recorder(pki):
    """HTTPS on 127.0.0.1 that accepts a client certificate signed by the test CA but does not demand
    one, so a test sees whether the channel presented it."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki["dir"] / "server.pem", pki["dir"] / "server.key")
    ctx.load_verify_locations(pki["dir"] / "ca.pem")
    ctx.verify_mode = ssl.CERT_OPTIONAL
    srv, handler = _recorder(ctx)
    yield srv.server_address[1], handler
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def http_recorder():
    srv, handler = _recorder()
    yield srv.server_address[1], handler
    srv.shutdown()
    srv.server_close()


def _http_channel(port, password="s3cret"):
    from atf.access.channels.ip import IpChannel
    from atf.core.inventory import parse
    board = parse({"boards": [{"name": "board-1", "creds": {"admin": {"user": "admin", "password_ref": "pw"}},
                               "drivers": {"mgmt": {"type": "ip", "ip": "127.0.0.1",
                                                    "restconf": {"scheme": "http", "role": "admin",
                                                                 "port": port}}}}]},
                  {"pw": password}).boards[0]
    return IpChannel(board.drivers["mgmt"], None, board.creds, tls=board.tls.get("mgmt"))


def test_restconf_request_patches_a_json_body_with_the_client_certificate(pki, tls_recorder):
    port, h = tls_recorder
    h.routes[("PATCH", "/restconf/data/acme:system/config")] = (204, None)

    status, body = _mtls(pki, port).restconf_request("PATCH", "acme:system/config",
                                                     {"acme:config": {"location": "rack-2"}})

    assert (status, body) == (204, None)
    [req] = h.seen
    assert req["method"] == "PATCH"
    assert json.loads(req["body"]) == {"acme:config": {"location": "rack-2"}}
    assert req["type"] == "application/yang-data+json"
    assert req["accept"] == "application/yang-data+json"
    assert req["cert"] is True and req["auth"] is None


def test_restconf_request_posts_with_the_role_credentials_over_http(http_recorder):
    import base64
    port, h = http_recorder
    h.routes[("POST", "/restconf/data/acme:system/install")] = (200, {"acme:output": {"job": 7}})

    status, body = _http_channel(port).restconf_request("POST", "acme:system/install",
                                                        {"acme:input": {"url": "http://192.0.2.9/b"}})

    assert (status, body) == (200, {"acme:output": {"job": 7}})
    assert h.seen[0]["auth"] == "Basic " + base64.b64encode(b"admin:s3cret").decode()


def test_restconf_request_put_and_delete_return_the_status_and_send_no_body_on_delete(http_recorder):
    port, h = http_recorder
    h.routes[("PUT", "/restconf/data/acme:users/user=u1")] = (201, None)
    h.routes[("DELETE", "/restconf/data/acme:users/user=u1")] = (204, None)
    ch = _http_channel(port)

    assert ch.restconf_request("PUT", "acme:users/user=u1", {"acme:user": [{"name": "u1"}]}) == (201, None)
    assert ch.restconf_request("DELETE", "acme:users/user=u1") == (204, None)
    assert h.seen[1]["body"] == "" and h.seen[1]["type"] is None


def test_restconf_request_returns_an_http_error_status_with_its_body(http_recorder):
    port, h = http_recorder
    h.routes[("PATCH", "/restconf/data/acme:system/config")] = (403, {"errors": "access denied"})

    assert _http_channel(port).restconf_request("PATCH", "acme:system/config", {"x": 1}) == \
        (403, {"errors": "access denied"})


def test_restconf_request_with_auth_sends_those_credentials_and_not_the_client_certificate(pki, tls_recorder):
    """`auth=(user, password)` authenticates as someone else than the bench role — e.g. an unknown user,
    to provoke a login failure — so the certificate, which would authenticate as the role, stays out."""
    import base64
    port, h = tls_recorder
    h.routes[("GET", "/restconf/data/acme:system")] = (401, {"errors": "unauthorized"})

    status, _ = _mtls(pki, port).restconf_request("GET", "acme:system", auth=("nobody", "wrong"))

    assert status == 401
    assert h.seen[0]["auth"] == "Basic " + base64.b64encode(b"nobody:wrong").decode()
    assert h.seen[0]["cert"] is False


def test_restconf_request_with_auth_false_sends_no_credentials(http_recorder):
    port, h = http_recorder
    h.routes[("GET", "/restconf/data/acme:system")] = (401, {"errors": "unauthorized"})

    assert _http_channel(port).restconf_request("GET", "acme:system", auth=False)[0] == 401
    assert h.seen[0]["auth"] is None


def test_restconf_request_takes_a_path_from_the_server_root_when_it_starts_with_a_slash(http_recorder):
    port, h = http_recorder
    h.routes[("POST", "/restconf/operations/acme:restart")] = (204, None)

    assert _http_channel(port).restconf_request("POST", "/restconf/operations/acme:restart") == (204, None)


def test_restconf_request_refuses_a_method_restconf_does_not_have(http_recorder):
    port, h = http_recorder

    with pytest.raises(ValueError, match="TRACE"):
        _http_channel(port).restconf_request("TRACE", "acme:system")
    assert h.seen == []


def test_restconf_get_still_reads_through_the_same_rules(http_recorder):
    port, h = http_recorder
    h.routes[("GET", "/restconf/data/acme:system")] = (200, {"ok": True})

    assert _http_channel(port).restconf_get("acme:system") == (200, {"ok": True})
    assert h.seen[0]["auth"].startswith("Basic ")


def test_a_mgmt_check_writes_restconf_through_the_worker(pki, tls_recorder, tmp_path):
    from atf.access.mgmt.dispatch import dispatch
    from atf.core.inventory import parse
    from atf.core.model import Result, Verdict
    from atf.core.registry import REGISTRY, register

    port, h = tls_recorder
    h.routes[("PATCH", "/restconf/data/acme:system/config")] = (204, None)

    @register(id="test-restconf-write", drivers=("mgmt",), title="writes one RESTCONF path")
    def _write(ctx):
        status, _ = ctx.mgmt.restconf_request("PATCH", "acme:system/config", {"acme:config": {"x": 1}})
        return Result(Verdict.PASS if status == 204 else Verdict.GAP, metrics={"status": status})

    board = parse({"boards": [{"name": "board-1", "drivers": {"mgmt": {
        "type": "ip", "ip": "127.0.0.1",
        "restconf": {"port": port, "cert_ref": "c", "key_ref": "k", "ca_ref": "ca"}}}}]},
        {"c": pki["client.pem"], "k": pki["client.key"], "ca": pki["ca.pem"]}).boards[0]
    try:
        [result] = dispatch(board, [REGISTRY["test-restconf-write"]], tmp_path, backend="local")
    finally:
        REGISTRY.pop("test-restconf-write", None)

    assert result["verdict"] == "pass", result["detail"]
    assert h.seen[0]["method"] == "PATCH" and h.seen[0]["cert"] is True
