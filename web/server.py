"""A local web page for talking to Shipcheck in the browser.

Serves the page in web/static and hands it a LiveKit token for a new room with the shipcheck
agent dispatched into it. Run the agent and this side by side:

    uv run src/agent.py dev     # terminal 1: the agent, registered with LiveKit Cloud
    uv run web/server.py        # terminal 2: then open http://localhost:8000

Open http://localhost:8000/?preview to see the page without connecting to anything.

This is for local use only. Anyone who can reach the token endpoint can start a session on
your LiveKit project, so it listens on 127.0.0.1 and nothing else.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import timedelta
from pathlib import Path

from aiohttp import web
from livekit import api

from shipcheck.config import settings  # also loads .env.local

STATIC = Path(__file__).resolve().parent / "static"
TOKEN_TTL = timedelta(minutes=10)


def mint_token(api_key: str, api_secret: str, agent_name: str) -> tuple[str, str]:
    """Return (room name, JWT) for a new room that dispatches ``agent_name`` when it opens."""
    room = f"shipcheck-web-{secrets.token_hex(4)}"
    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity(f"browser-{secrets.token_hex(4)}")
        .with_name("Browser")
        .with_ttl(TOKEN_TTL)
        .with_grants(
            api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True)
        )
        .with_room_config(
            api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=agent_name)])
        )
        .to_jwt()
    )
    return room, token


def create_app(url: str, api_key: str, api_secret: str, agent_name: str) -> web.Application:
    async def index(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC / "index.html")

    async def token(_: web.Request) -> web.Response:
        room, jwt = mint_token(api_key, api_secret, agent_name)
        return web.json_response(
            {"serverUrl": url, "roomName": room, "participantToken": jwt},
            headers={"Cache-Control": "no-store"},
        )

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_post("/api/token", token)
    app.router.add_static("/static", STATIC)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    creds = {
        k: os.environ.get(k, "") for k in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    }
    missing = [k for k, v in creds.items() if not v]
    if missing:
        sys.exit(f"Missing {', '.join(missing)}. Run `lk app env -w` to write .env.local.")

    app = create_app(
        creds["LIVEKIT_URL"],
        creds["LIVEKIT_API_KEY"],
        creds["LIVEKIT_API_SECRET"],
        settings.agent_name,
    )
    print(f"Shipcheck web: http://localhost:{args.port}  (agent: {settings.agent_name})")
    web.run_app(app, host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
