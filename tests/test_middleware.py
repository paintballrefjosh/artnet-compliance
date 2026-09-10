"""The CSRF/auth gate in create_app, driven through the real ASGI stack.

httpx isn't a dependency, so these speak ASGI directly rather than using
starlette's TestClient.
"""
import asyncio
import json

from artnet_monitor.config import load_config
from artnet_monitor.engine import ComplianceEngine
from artnet_monitor.fixture_library.store import FixtureLibraryStore
from artnet_monitor.web.app import create_app


def call(app, method="GET", path="/api/snapshot", headers=None, query=b""):
    """Drives one request through the ASGI app; returns (status, body)."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "path": path, "raw_path": path.encode(),
        "query_string": query, "headers": raw_headers, "scheme": "http",
        "server": ("127.0.0.1", 8000), "client": ("127.0.0.1", 9999), "root_path": "",
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, body


def build(tmp_path, auth_token=None):
    path = tmp_path / "patch.yaml"
    path.write_text('listen_ip: "1.2.3.4"\nuniverses: [1]\nfixtures: []\n', encoding="utf-8")
    library = FixtureLibraryStore(str(tmp_path))
    engine = ComplianceEngine(load_config(str(path), library=library))

    class _Manager:
        config = engine.config

    return create_app(engine, _Manager(), library, str(path), auth_token=auth_token)


def test_same_origin_write_is_allowed(tmp_path):
    app = build(tmp_path)
    status, _ = call(app, "POST", "/api/active-cue",
                     headers={"host": "localhost:8000", "origin": "http://localhost:8000",
                              "content-type": "application/json"})
    assert status != 403


def test_cross_origin_write_is_refused(tmp_path):
    """The attack this blocks: a page on another site POSTing multipart to
    localhost, which needs no CORS preflight."""
    app = build(tmp_path)
    status, body = call(app, "POST", "/api/backup/import",
                        headers={"host": "localhost:8000", "origin": "http://evil.example"})
    assert status == 403
    assert "Cross-origin" in json.loads(body)["error"]


def test_cross_origin_read_is_still_allowed(tmp_path):
    """Only state-changing methods are gated; a GET can't be read
    cross-origin anyway without CORS headers, which aren't sent."""
    app = build(tmp_path)
    status, _ = call(app, "GET", "/api/snapshot",
                     headers={"host": "localhost:8000", "origin": "http://evil.example"})
    assert status == 200


def test_no_origin_header_passes(tmp_path):
    """curl and scripts send no Origin and must keep working."""
    app = build(tmp_path)
    status, _ = call(app, "POST", "/api/active-cue", headers={"host": "localhost:8000"})
    assert status != 403


def test_auth_token_rejects_requests_without_it(tmp_path):
    app = build(tmp_path, auth_token="s3cret")
    status, body = call(app, "GET", "/api/snapshot", headers={"host": "localhost:8000"})
    assert status == 401
    assert "Authentication" in json.loads(body)["error"]


def test_auth_token_accepts_the_header(tmp_path):
    app = build(tmp_path, auth_token="s3cret")
    status, _ = call(app, "GET", "/api/snapshot",
                     headers={"host": "localhost:8000", "x-auth-token": "s3cret"})
    assert status == 200


def test_auth_token_accepts_the_query_param(tmp_path):
    app = build(tmp_path, auth_token="s3cret")
    status, _ = call(app, "GET", "/api/snapshot",
                     headers={"host": "localhost:8000"}, query=b"token=s3cret")
    assert status == 200


def test_auth_token_accepts_the_cookie(tmp_path):
    app = build(tmp_path, auth_token="s3cret")
    status, _ = call(app, "GET", "/api/snapshot",
                     headers={"host": "localhost:8000", "cookie": "artnet_token=s3cret"})
    assert status == 200


def test_wrong_token_is_rejected(tmp_path):
    app = build(tmp_path, auth_token="s3cret")
    status, _ = call(app, "GET", "/api/snapshot",
                     headers={"host": "localhost:8000", "x-auth-token": "wrong"})
    assert status == 401
