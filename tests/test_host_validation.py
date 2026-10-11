"""The local HTTP boundary must reject DNS rebinding before any route runs."""
from conftest import authorize_host
import asyncio
from importlib.resources import files

import httpx
import pytest
from fastapi.testclient import TestClient
from conftest import login_admin

from iris.api import create_app
from test_retrieval import put


ASSET = next(files("iris").joinpath("web", "assets").iterdir()).name
READ_ROUTES = [
    ("GET", "/admin/api/memories"),
    ("POST", "/api/v1/memories/search"),
    ("GET", "/admin/api/status"),
    ("GET", "/api/v1/status"),
    ("GET", "/"),
    ("GET", f"/assets/{ASSET}"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
]


@pytest.fixture
def client(store):
    with TestClient(create_app(store=store), base_url="http://127.0.0.1", client=("127.0.0.1", 1000)) as client:
        authorize_host(client)
        login_admin(client)
        client.app.state.scheduler.stop()
        yield client


def read(client, method, path, host):
    return client.request(method, path, headers={"Host": host},
                          **({"json": {}} if method == "POST" else {}))


def assert_rejected(response):
    assert response.status_code == 400
    if response.request.url.path.startswith("/api/v1/"):
        error = response.json()["error"]
        assert error["code"] == "invalid_request"
        assert error["fields"][0]["field"] == "header.host"
    else:
        assert response.text == "Invalid host header"
    assert "location" not in response.headers
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("method,path", READ_ROUTES)
def test_foreign_host_cannot_read_apis_or_static_files(client, store, method, path):
    put(store, "仅本机可读的私密记忆")
    assert_rejected(read(client, method, path, "evil.example:8080"))
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM recalls").fetchone()[0] == 0


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:8080", "localhost", "localhost:8080", "[::1]", "[::1]:8080",
])
def test_loopback_hosts_with_optional_ports_serve_all_routes(client, store, host):
    mid = put(store, "仅本机可读的私密记忆")
    for method, path in READ_ROUTES:
        response = read(client, method, path, host)
        assert response.status_code == 200, (method, path, response.text)
        if path == "/admin/api/memories":
            assert response.json()["items"][0]["id"] == mid
        elif path == "/api/v1/memories/search":
            assert response.json()["memories"][0]["id"] == mid


@pytest.mark.parametrize("host", [
    "evil.example", "localhost.evil.example:8080", "127.0.0.1.evil.example:8080",
    "www.localhost:8080", "192.168.1.2:8080", "localhost@evil.example:8080",
])
def test_host_is_an_exact_allowlist_not_resolved_or_taken_from_proxy_headers(client, host):
    assert_rejected(client.get("/admin/api/status", headers={
        "Host": host, "X-Forwarded-Host": "localhost:8080", "Forwarded": "host=localhost:8080",
        "Origin": "http://localhost:8080",
    }))


def test_foreign_host_cannot_send_edit_or_delete(client, store):
    mid = put(store, "必须保留的记忆")
    entry = client.post("/admin/api/trial/entries", json={"name": "本机试用"}).json()
    headers = {"Host": "evil.example:8080"}
    writes = [
        ("POST", "/api/v1/entries/A/messages", {
            "sender": "外部网页", "content": "不得入队", "occurred_at": "2026-10-07T10:00:00+08:00",
            "dedupe_key": "host-attack",
        }),
        ("POST", f"/admin/api/trial/entries/{entry['id']}/messages", {
            "speaker_id": entry["default_speaker_id"], "content": "不得入队", "dedupe_key": "admin-attack",
        }),
        ("PATCH", f"/admin/api/memories/{mid}", {"expected_revision": 1, "content": "不得修改"}),
        ("DELETE", f"/admin/api/memories/{mid}", {"expected_revision": 1}),
    ]
    for method, path, payload in writes:
        assert_rejected(client.request(method, path, json=payload, headers=headers))
    with store.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 0
        row = conn.execute("SELECT content,lifecycle,revision FROM memories WHERE id=?", (mid,)).fetchone()
        assert tuple(row) == ("必须保留的记忆", "active", 1)


def test_missing_host_is_rejected_before_readiness(client):
    client.app.state.ready = False
    assert_rejected(client.get("/api/v1/status", headers={"Host": "evil.example:8080"}))
    # TestClient inserts a missing Host; raw ASGI transport preserves its absence.
    async def without_host():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.app),
                                     base_url="http://127.0.0.1") as raw:
            request = raw.build_request("GET", "/")
            del request.headers["Host"]
            return await raw.send(request)
    assert_rejected(asyncio.run(without_host()))
    assert client.get("/api/v1/status").status_code == 503
