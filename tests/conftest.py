"""Shared fixtures: canned upstream payloads served by a local fake.

No test in this suite talks to PyPI, OSV or Anaconda: the upstream base URLs are
pointed at an in-process aiohttp server that answers only what a test registers.
Tests marked ``llm`` use a real LLM through LiveKit Inference (text only) and
are skipped without credentials.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from shipcheck import upstreams

PYPI = "/pypi"
OSV = "/v1/query"
CONDA = "/release/main"
ANY_PYPI = "/pypi/*"
ANY_CONDA = "/release/main/*"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("LIVEKIT_API_KEY") and os.environ.get("LIVEKIT_API_SECRET"):
        return
    skip = pytest.mark.skip(reason="LIVEKIT_API_KEY / LIVEKIT_API_SECRET not set")
    for item in items:
        if "llm" in item.keywords:
            item.add_marker(skip)


@dataclass
class _Reply:
    status: int
    payload: Any
    delay: float
    repeat: bool


class FakeUpstreams:
    """Programmable stand-in for PyPI, OSV and Anaconda.

    Replies registered for a path are served in order; a reply registered with
    ``repeat=True`` is served forever. A path ending in ``*`` matches by prefix.
    Anything unregistered gets a 599 and is listed in ``unexpected``.
    """

    def __init__(self) -> None:
        self._replies: dict[tuple[str, str], list[_Reply]] = {}
        self.requests: list[tuple[str, str]] = []
        self.unexpected: list[tuple[str, str]] = []

    def _add(
        self, method: str, path: str, status: int, payload: Any, delay: float, repeat: bool
    ) -> None:
        self._replies.setdefault((method, path), []).append(_Reply(status, payload, delay, repeat))

    def get(
        self,
        path: str,
        *,
        status: int = 200,
        payload: Any = None,
        delay: float = 0,
        repeat: bool = False,
    ) -> None:
        self._add("GET", path, status, payload, delay, repeat)

    def post(
        self,
        path: str,
        *,
        status: int = 200,
        payload: Any = None,
        delay: float = 0,
        repeat: bool = False,
    ) -> None:
        self._add("POST", path, status, payload, delay, repeat)

    def slow_down(self, delay: float) -> None:
        """Make every registered reply take ``delay`` seconds."""
        for queue in self._replies.values():
            for reply in queue:
                reply.delay = delay

    def paths(self, prefix: str) -> list[str]:
        return [path for _, path in self.requests if path.startswith(prefix)]

    async def handle(self, request: web.Request) -> web.StreamResponse:
        key = (request.method, request.path)
        self.requests.append(key)
        queue = self._replies.get(key)
        if queue is None:
            for (method, pattern), candidate in self._replies.items():
                if (
                    method == request.method
                    and pattern.endswith("*")
                    and request.path.startswith(pattern[:-1])
                ):
                    queue = candidate
                    break
        if not queue:
            self.unexpected.append(key)
            return web.json_response({"error": "not mocked"}, status=599)
        reply = queue[0] if queue[0].repeat else queue.pop(0)
        if reply.delay:
            await asyncio.sleep(reply.delay)
        if reply.payload is None:
            return web.Response(status=reply.status)
        return web.json_response(reply.payload, status=reply.status)


@pytest.fixture
async def http(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeUpstreams]:
    fake = FakeUpstreams()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    server = TestServer(app)
    await server.start_server()
    base = f"http://127.0.0.1:{server.port}"
    monkeypatch.setattr(upstreams, "PYPI_URL", base + PYPI)
    monkeypatch.setattr(upstreams, "OSV_URL", base + OSV)
    monkeypatch.setattr(upstreams, "CONDA_URL", base + CONDA)
    try:
        yield fake
    finally:
        await server.close()


def pypi_project(latest: str, upload: str = "2026-05-14T19:25:26.443000Z") -> dict[str, Any]:
    return {
        "info": {"version": latest, "yanked": False, "yanked_reason": None},
        "urls": [{"upload_time_iso_8601": upload, "yanked": False}],
    }


def pypi_release(
    version: str, upload: str = "2018-06-12T14:46:00.000000Z", *, yanked: bool = False
) -> dict[str, Any]:
    return {
        "info": {
            "version": version,
            "yanked": yanked,
            "yanked_reason": "broken build" if yanked else None,
            "requires_python": ">=3.9",
        },
        "urls": [{"upload_time_iso_8601": upload, "yanked": yanked}],
    }


def osv_vuln(
    vuln_id: str,
    name: str,
    fixed: str | None,
    *,
    aliases: list[str] | None = None,
    severity: str | None = None,
    summary: str = "Something bad",
    withdrawn: str | None = None,
) -> dict[str, Any]:
    events: list[dict[str, str]] = [{"introduced": "0"}]
    if fixed:
        events.append({"fixed": fixed})
    vuln: dict[str, Any] = {
        "id": vuln_id,
        "summary": summary,
        "aliases": aliases or [],
        "affected": [
            {
                "package": {"name": name, "ecosystem": "PyPI"},
                "ranges": [{"type": "ECOSYSTEM", "events": events}],
            }
        ],
    }
    if severity:
        vuln["database_specific"] = {"severity": severity}
    if withdrawn:
        vuln["withdrawn"] = withdrawn
    return vuln


def conda_release(name: str, version: str, subdirs: list[str]) -> dict[str, Any]:
    return {
        "distributions": [
            {
                "basename": f"{subdir}/{name}-{version}-py312h0_0.conda",
                "version": version,
                "attrs": {"subdir": subdir, "build": "py312h0_0"},
            }
            for subdir in subdirs
        ]
    }


def mock_requests_2_19(http: FakeUpstreams) -> None:
    """requests 2.19.0: exists, vulnerable, fixed in 2.31.0, not on conda defaults."""
    http.get(f"{PYPI}/requests/json", payload=pypi_project("2.34.2"), repeat=True)
    http.get(f"{PYPI}/requests/2.19.0/json", payload=pypi_release("2.19.0"), repeat=True)
    vulns = [
        osv_vuln("GHSA-aaaa", "requests", "2.20.0", aliases=["CVE-2018-18074"], severity="HIGH"),
        osv_vuln("PYSEC-2018-28", "requests", "2.20.0", aliases=["CVE-2018-18074"]),
        osv_vuln(
            "GHSA-bbbb", "requests", "2.31.0", aliases=["CVE-2023-32681"], severity="MODERATE"
        ),
    ]
    # First query: the stated version. Second: is the fix version clean?
    http.post(OSV, payload={"vulns": vulns})
    http.post(OSV, payload={})
    http.get(
        f"{CONDA}/requests/2.19.0",
        status=404,
        payload={"error": "release version='2.19.0' does not exist"},
        repeat=True,
    )


def mock_numpy_2_2_4(http: FakeUpstreams) -> None:
    """numpy 2.2.4: exists, clean, on conda defaults for five platforms (no linux-aarch64)."""
    http.get(
        f"{PYPI}/numpy/json", payload=pypi_project("2.5.5", "2026-11-15T10:00:00Z"), repeat=True
    )
    http.get(
        f"{PYPI}/numpy/2.2.4/json",
        payload=pypi_release("2.2.4", "2025-03-16T10:00:00Z"),
        repeat=True,
    )
    http.post(OSV, payload={}, repeat=True)
    http.get(
        f"{CONDA}/numpy/2.2.4",
        payload=conda_release("numpy", "2.2.4", ["linux-64", "osx-64", "osx-arm64", "win-64"]),
        repeat=True,
    )


def mock_torch_2_5_1(http: FakeUpstreams) -> None:
    """torch 2.5.1: exists, vulnerable with a fix in 2.6.0, on conda as pytorch incl. aarch64."""
    http.get(
        f"{PYPI}/torch/json", payload=pypi_project("2.15.0", "2026-10-28T10:00:00Z"), repeat=True
    )
    http.get(
        f"{PYPI}/torch/2.5.1/json",
        payload=pypi_release("2.5.1", "2024-10-29T10:00:00Z"),
        repeat=True,
    )
    http.post(
        OSV,
        payload={
            "vulns": [
                osv_vuln(
                    "GHSA-cccc",
                    "torch",
                    "2.6.0",
                    aliases=["CVE-2025-32434"],
                    severity="CRITICAL",
                    summary="torch.load remote code execution",
                )
            ]
        },
    )
    http.post(OSV, payload={})
    http.get(
        f"{CONDA}/pytorch/2.5.1",
        payload=conda_release(
            "pytorch", "2.5.1", ["linux-64", "linux-aarch64", "osx-arm64", "win-64"]
        ),
        repeat=True,
    )
