"""The local web server: the token it hands the browser, and the page it serves.

Offline: tokens are signed locally with made-up credentials and nothing connects to LiveKit.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from livekit import api

spec = importlib.util.spec_from_file_location(
    "web_server", Path(__file__).resolve().parent.parent / "web" / "server.py"
)
assert spec and spec.loader
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

URL = "ws://127.0.0.1:1"
KEY = "test-key"
SECRET = "test-secret-that-is-long-enough-for-hs256"


async def _client() -> TestClient:
    client = TestClient(TestServer(server.create_app(URL, KEY, SECRET, "shipcheck")))
    await client.start_server()
    return client


async def test_token_joins_a_new_room_and_dispatches_the_agent() -> None:
    client = await _client()
    try:
        res = await client.post("/api/token")
        assert res.status == 200
        assert res.headers["Cache-Control"] == "no-store"
        body = await res.json()
    finally:
        await client.close()

    assert body["serverUrl"] == URL
    claims = api.TokenVerifier(KEY, SECRET).verify(body["participantToken"])
    assert claims.video is not None
    assert claims.video.room_join is True
    assert claims.video.room == body["roomName"]
    assert body["roomName"].startswith("shipcheck-web-")
    assert claims.video.can_publish is True
    assert claims.video.can_subscribe is True
    assert claims.room_config is not None
    assert [a.agent_name for a in claims.room_config.agents] == ["shipcheck"]


async def test_each_token_gets_its_own_room_and_identity() -> None:
    first = server.mint_token(KEY, SECRET, "shipcheck")
    second = server.mint_token(KEY, SECRET, "shipcheck")
    assert first[0] != second[0]
    verifier = api.TokenVerifier(KEY, SECRET)
    assert verifier.verify(first[1]).identity != verifier.verify(second[1]).identity


async def test_token_endpoint_only_accepts_post() -> None:
    client = await _client()
    try:
        res = await client.get("/api/token")
        assert res.status == 405
    finally:
        await client.close()


async def test_serves_the_page_and_its_assets() -> None:
    client = await _client()
    try:
        page = await client.get("/")
        assert page.status == 200
        html = await page.text()
        assert "Start talking to agent" in html
        for asset in ("/static/app.js", "/static/blob.js", "/static/style.css"):
            res = await client.get(asset)
            assert res.status == 200, asset
    finally:
        await client.close()
