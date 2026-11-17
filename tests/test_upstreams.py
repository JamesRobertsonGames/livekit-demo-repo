"""Upstream clients with HTTP mocked: happy paths, failures, timeouts, version maths."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest

from shipcheck import upstreams
from shipcheck.upstreams import (
    conda_name_candidates,
    fetch_conda,
    fetch_osv,
    fetch_pypi,
    fetch_pypi_latest,
)

from .conftest import (
    ANY_CONDA,
    ANY_PYPI,
    CONDA,
    OSV,
    PYPI,
    FakeUpstreams,
    conda_release,
    mock_requests_2_19,
    osv_vuln,
    pypi_project,
    pypi_release,
)


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as s:
        yield s


# ----------------------------------------------------------------------------- PyPI


async def test_pypi_ok(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    mock_requests_2_19(http)
    r = await fetch_pypi(session, "Requests", "2.19.0")
    assert r["status"] == "ok"
    assert r["package"] == "requests"
    assert r["latest_version"] == "2.34.2"
    assert r["latest_release_date"] == "2026-05-14"
    assert r["stated_version_exists"] is True
    assert r["stated_version_release_date"] == "2018-06-12"
    assert r["stated_version_is_latest"] is False
    assert r["stated_version_yanked"] is False


async def test_pypi_version_that_was_never_published(
    http: FakeUpstreams, session: aiohttp.ClientSession
) -> None:
    http.get(f"{PYPI}/requests/json", payload=pypi_project("2.34.2"))
    http.get(f"{PYPI}/requests/9.9.9/json", status=404, payload={"message": "Not Found"})
    r = await fetch_pypi(session, "requests", "9.9.9")
    assert r["status"] == "ok"
    assert r["stated_version_exists"] is False
    assert r["latest_version"] == "2.34.2"
    assert "stated_version_release_date" not in r


async def test_pypi_unknown_package(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    http.get(ANY_PYPI, status=404, payload={"message": "Not Found"}, repeat=True)
    r = await fetch_pypi(session, "blorptastic", "1.0")
    assert r["status"] == "package_not_found"
    assert "latest_version" not in r


async def test_pypi_yanked(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    http.get(f"{PYPI}/foo/json", payload=pypi_project("2.0"))
    http.get(f"{PYPI}/foo/1.0/json", payload=pypi_release("1.0", yanked=True))
    r = await fetch_pypi(session, "foo", "1.0")
    assert r["stated_version_yanked"] is True
    assert r["yanked_reason"] == "broken build"


async def test_pypi_latest(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    http.get(f"{PYPI}/requests/json", payload=pypi_project("2.34.2"))
    r = await fetch_pypi_latest(session, "requests")
    assert r["status"] == "ok" and r["stated_version"] == "2.34.2"


@pytest.mark.parametrize("status", [500, 502, 503, 429])
async def test_pypi_http_error_is_unavailable(
    http: FakeUpstreams, session: aiohttp.ClientSession, status: int
) -> None:
    http.get(ANY_PYPI, status=status, repeat=True)
    r = await fetch_pypi(session, "requests", "2.19.0")
    assert r["status"] == "unavailable"
    assert "latest_version" not in r


async def test_pypi_timeout_is_unavailable(
    http: FakeUpstreams, session: aiohttp.ClientSession
) -> None:
    http.get(ANY_PYPI, delay=5, repeat=True)
    started = asyncio.get_running_loop().time()
    r = await fetch_pypi(session, "requests", "2.19.0", timeout=0.1)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert r == {**r, "status": "unavailable", "reason": "timeout"}


async def test_pypi_garbage_payload_is_unavailable(
    http: FakeUpstreams, session: aiohttp.ClientSession
) -> None:
    http.get(ANY_PYPI, payload={"unexpected": True}, repeat=True)
    r = await fetch_pypi(session, "requests", "2.19.0")
    assert r["status"] == "unavailable"


async def test_pypi_connection_error_is_unavailable(
    session: aiohttp.ClientSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(upstreams, "PYPI_URL", "http://127.0.0.1:1/pypi")  # nothing listens here
    r = await fetch_pypi(session, "requests", "2.19.0")
    assert r["status"] == "unavailable"


@pytest.mark.parametrize("fetch", [fetch_pypi, fetch_osv, fetch_conda])
async def test_invalid_version_never_reaches_the_network(
    http: FakeUpstreams, session: aiohttp.ClientSession, fetch: Any
) -> None:
    r = await fetch(session, "requests", "banana")
    assert r["status"] == "invalid_version"
    assert not http.requests


# ------------------------------------------------------------------------------ OSV


async def test_osv_groups_aliases_and_finds_first_fixed_version(
    http: FakeUpstreams, session: aiohttp.ClientSession
) -> None:
    mock_requests_2_19(http)
    r = await fetch_osv(session, "requests", "2.19.0")
    assert r["status"] == "ok"
    assert r["vulnerable"] is True
    assert r["vulnerability_count"] == 2  # GHSA-aaaa and PYSEC-2018-28 are the same issue
    assert r["highest_severity"] == "HIGH"
    assert r["first_fixed_version"] == "2.31.0"
    assert r["first_fixed_version_is_clean"] is True
    assert r["without_a_fix"] == 0


async def test_osv_clean(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    http.post(OSV, payload={})
    r = await fetch_osv(session, "numpy", "2.2.4")
    assert r["status"] == "ok"
    assert r["vulnerable"] is False
    assert r["vulnerability_count"] == 0
    assert "first_fixed_version" not in r


async def test_osv_no_fix_available_names_no_upgrade_target(
    http: FakeUpstreams, session: aiohttp.ClientSession
) -> None:
    http.post(
        OSV,
        payload={
            "vulns": [
                osv_vuln("GHSA-1", "foo", "1.5.0", severity="LOW"),
                osv_vuln("GHSA-2", "foo", None, severity="CRITICAL"),
            ]
        },
    )
    r = await fetch_osv(session, "foo", "1.0.0")
    assert r["vulnerable"] is True
    assert r["first_fixed_version"] is None
    assert r["without_a_fix"] == 1
    assert r["highest_severity"] == "CRITICAL"


async def test_osv_fix_version_with_its_own_vulnerabilities_is_flagged(
    http: FakeUpstreams, session: aiohttp.ClientSession
) -> None:
    http.post(OSV, payload={"vulns": [osv_vuln("GHSA-1", "foo", "1.5.0")]})
    http.post(OSV, payload={"vulns": [osv_vuln("GHSA-9", "foo", "2.0.0")]})
    r = await fetch_osv(session, "foo", "1.0.0")
    assert r["first_fixed_version"] == "1.5.0"
    assert r["first_fixed_version_is_clean"] is False


async def test_osv_ignores_withdrawn_and_other_packages(
    http: FakeUpstreams, session: aiohttp.ClientSession
) -> None:
    other = osv_vuln("GHSA-3", "not-foo", "9.9.9")
    other["affected"].append(
        {
            "package": {"name": "Foo", "ecosystem": "PyPI"},
            "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}, {"fixed": "1.2.0"}]}],
        }
    )
    http.post(
        OSV,
        payload={
            "vulns": [osv_vuln("GHSA-w", "foo", "5.0.0", withdrawn="2024-01-01T00:00:00Z"), other]
        },
    )
    http.post(OSV, payload={})
    r = await fetch_osv(session, "foo", "1.0.0")
    assert r["vulnerability_count"] == 1
    assert r["first_fixed_version"] == "1.2.0"  # not 5.0.0 (withdrawn) or 9.9.9 (other package)


async def test_osv_follows_pagination(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    http.post(OSV, payload={"vulns": [osv_vuln("GHSA-1", "foo", "1.1.0")], "next_page_token": "t"})
    http.post(OSV, payload={"vulns": [osv_vuln("GHSA-2", "foo", "1.4.0")]})
    http.post(OSV, payload={})
    r = await fetch_osv(session, "foo", "1.0.0")
    assert r["vulnerability_count"] == 2
    assert r["first_fixed_version"] == "1.4.0"


async def test_osv_failure_reports_no_vulnerability_facts(
    http: FakeUpstreams, session: aiohttp.ClientSession
) -> None:
    http.post(OSV, status=503)
    r = await fetch_osv(session, "requests", "2.19.0")
    assert r["status"] == "unavailable"
    assert "vulnerable" not in r and "first_fixed_version" not in r


async def test_osv_timeout(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    http.post(OSV, delay=5)
    r = await fetch_osv(session, "requests", "2.19.0", timeout=0.1)
    assert r["status"] == "unavailable" and r["reason"] == "timeout"


# ---------------------------------------------------------------------------- conda


async def test_conda_platforms(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    http.get(
        f"{CONDA}/numpy/2.2.4",
        payload=conda_release("numpy", "2.2.4", ["linux-64", "osx-arm64", "linux-64"]),
    )
    r = await fetch_conda(session, "numpy", "2.2.4")
    assert r["status"] == "ok"
    assert r["available"] is True
    assert r["platforms"] == ["linux-64", "osx-arm64"]
    assert r["python_builds"]["linux-64"] == ["3.12"]


async def test_conda_uses_the_conda_name(
    http: FakeUpstreams, session: aiohttp.ClientSession
) -> None:
    http.get(f"{CONDA}/pytorch/2.5.1", payload=conda_release("pytorch", "2.5.1", ["linux-aarch64"]))
    r = await fetch_conda(session, "torch", "2.5.1")
    assert r["conda_name"] == "pytorch"
    assert r["platforms"] == ["linux-aarch64"]


async def test_conda_version_missing(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    mock_requests_2_19(http)
    r = await fetch_conda(session, "requests", "2.19.0")
    assert r["status"] == "version_not_found"
    assert r["available"] is False
    assert "platforms" not in r


async def test_conda_package_missing(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    http.get(ANY_CONDA, status=404, payload={"error": '"x" could not be found'}, repeat=True)
    r = await fetch_conda(session, "some-thing", "1.0")
    assert r["status"] == "package_not_found"
    assert http.paths(CONDA) == [f"{CONDA}/{n}/1.0" for n in conda_name_candidates("some-thing")]


async def test_conda_failure(http: FakeUpstreams, session: aiohttp.ClientSession) -> None:
    http.get(ANY_CONDA, status=500, repeat=True)
    r = await fetch_conda(session, "numpy", "2.2.4")
    assert r["status"] == "unavailable"
    assert "platforms" not in r
