"""Clients for the three live data sources.

Each ``fetch_*`` coroutine takes an aiohttp session and returns a plain dict
that goes straight to the LLM. They never raise for upstream trouble: a
timeout, HTTP error or malformed payload becomes ``{"status": "unavailable"}``
so the agent can say so instead of guessing. Every fact in the result comes
from the upstream response; nothing is inferred beyond version arithmetic.

Endpoints (verified November 2026):

* PyPI JSON API      https://docs.pypi.org/api/json/
    GET /pypi/<project>/json            latest version metadata
    GET /pypi/<project>/<version>/json  one release
* OSV.dev            https://google.github.io/osv.dev/post-v1-query/
    POST /v1/query  {"package": {"name", "ecosystem": "PyPI"}, "version"}
* Anaconda defaults  https://repo.anaconda.com/pkgs/main is the documented
    channel, but its repodata is 15 MB+ per platform. Per-release metadata comes
    from api.anaconda.org GET /release/main/<name>/<version>, the endpoint
    Anaconda's own client (anaconda-client, ``Binstar.release``) uses. It has no
    public reference page; scripts/verify_upstreams.py cross-checks it against
    repo.anaconda.com.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp
from packaging.version import InvalidVersion, Version

from .names import normalize_name

logger = logging.getLogger("shipcheck.upstreams")

PYPI_URL = "https://pypi.org/pypi"
OSV_URL = "https://api.osv.dev/v1/query"
CONDA_URL = "https://api.anaconda.org/release/main"

# Hard wall-clock budget for one tool call, including retries and pagination.
TIMEOUT_S = float(os.environ.get("SHIPCHECK_UPSTREAM_TIMEOUT_S", "4.0"))
USER_AGENT = os.environ.get("SHIPCHECK_USER_AGENT", "shipcheck-voice-agent/0.1")

# PyPI project name -> conda package name on the defaults channel, where they differ.
CONDA_NAMES: dict[str, str] = {
    "torch": "pytorch",
    "opencv-python": "opencv",
    "opencv-python-headless": "opencv",
    "msgpack": "msgpack-python",
    "tables": "pytables",
    "pyqt5": "pyqt",
    "graphviz": "python-graphviz",
    "docker": "docker-py",
    "build": "python-build",
    "kaleido": "python-kaleido",
    "tensorflow-cpu": "tensorflow",
    "faiss-cpu": "faiss-cpu",
    "pyzmq": "pyzmq",
    "duckdb": "python-duckdb",
    "psycopg2-binary": "psycopg2",
    "matplotlib": "matplotlib-base",
    "dask": "dask-core",
    "prompt-toolkit": "prompt_toolkit",
    "importlib-metadata": "importlib_metadata",
    "importlib-resources": "importlib_resources",
    "ruamel-yaml": "ruamel.yaml",
    "backports-zoneinfo": "backports.zoneinfo",
}

_SEVERITY_ORDER = ["LOW", "MODERATE", "MEDIUM", "HIGH", "CRITICAL"]
_PY_BUILD = re.compile(r"(?:^|_)py(\d)(\d+)")


class _Unavailable(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def parse_version(version: str) -> Version | None:
    try:
        return Version(version.strip().lstrip("vV"))
    except InvalidVersion:
        return None


def _unavailable(source: str, reason: str) -> dict[str, Any]:
    return {
        "source": source,
        "status": "unavailable",
        "reason": reason,
        "note": f"{source} could not be reached. Do not state or guess anything it would have "
        "told you; tell the caller this part is unknown right now.",
    }


async def _guarded(
    source: str, work: Callable[[], Awaitable[dict[str, Any]]], timeout: float | None
) -> dict[str, Any]:
    """Run ``work`` under a hard deadline, turning every failure into a result dict."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(TIMEOUT_S if timeout is None else timeout):
            result = await work()
    except TimeoutError:
        result = _unavailable(source, "timeout")
    except _Unavailable as e:
        result = _unavailable(source, e.reason)
    except aiohttp.ClientError as e:
        result = _unavailable(source, f"network error: {type(e).__name__}")
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        logger.warning("unexpected %s payload: %r", source, e)
        result = _unavailable(source, "unexpected response format")
    result["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
    return result


async def _get_json(session: aiohttp.ClientSession, url: str) -> tuple[int, Any]:
    async with session.get(url, headers={"User-Agent": USER_AGENT}) as resp:
        if resp.status == 404:
            return 404, await resp.json(content_type=None)
        if resp.status != 200:
            raise _Unavailable(f"http {resp.status}")
        return 200, await resp.json(content_type=None)


def _first_upload(files: list[dict[str, Any]]) -> str | None:
    times = [f["upload_time_iso_8601"] for f in files if f.get("upload_time_iso_8601")]
    return min(times)[:10] if times else None


# --------------------------------------------------------------------------- PyPI


async def fetch_pypi(
    session: aiohttp.ClientSession, package: str, version: str, *, timeout: float | None = None
) -> dict[str, Any]:
    """Latest version and release dates from the PyPI JSON API."""
    name = normalize_name(package)

    async def work() -> dict[str, Any]:
        parsed = parse_version(version)
        if parsed is None:
            return {"source": "pypi", "status": "invalid_version", "package": name,
                    "note": f"'{version}' is not a valid version number. Ask the caller to repeat it."}  # fmt: skip
        (p_status, project), (r_status, release) = await asyncio.gather(
            _get_json(session, f"{PYPI_URL}/{name}/json"),
            _get_json(session, f"{PYPI_URL}/{name}/{parsed}/json"),
        )
        if p_status == 404:
            return {"source": "pypi", "status": "package_not_found", "package": name,
                    "note": "No project with this name exists on PyPI. Ask the caller to spell it."}  # fmt: skip
        latest = project["info"]["version"]
        out: dict[str, Any] = {
            "source": "pypi",
            "status": "ok",
            "package": name,
            "latest_version": latest,
            "latest_release_date": _first_upload(project.get("urls", [])),
        }
        if r_status == 404:
            out["stated_version"] = str(parsed)
            out["stated_version_exists"] = False
            out["note"] = (
                f"Version {parsed} of {name} was never published on PyPI. Say so and offer the "
                "latest version instead. Do not report vulnerability or conda results for it."
            )
            return out
        info = release["info"]
        out.update(
            stated_version=info["version"],
            stated_version_exists=True,
            stated_version_release_date=_first_upload(release.get("urls", [])),
            stated_version_is_latest=parse_version(info["version"]) == parse_version(latest),
            stated_version_yanked=bool(info.get("yanked")),
        )
        if info.get("yanked"):
            out["yanked_reason"] = info.get("yanked_reason") or "no reason given"
        if info.get("requires_python"):
            out["requires_python"] = info["requires_python"]
        return out

    return await _guarded("pypi", work, timeout)


async def fetch_pypi_latest(
    session: aiohttp.ClientSession, package: str, *, timeout: float | None = None
) -> dict[str, Any]:
    """The latest version of a project, for callers who ask about "latest"."""
    name = normalize_name(package)

    async def work() -> dict[str, Any]:
        status, project = await _get_json(session, f"{PYPI_URL}/{name}/json")
        if status == 404:
            return {"source": "pypi", "status": "package_not_found", "package": name,
                    "note": "No project with this name exists on PyPI. Ask the caller to spell it."}  # fmt: skip
        info = project["info"]
        return {
            "source": "pypi",
            "status": "ok",
            "package": name,
            "latest_version": info["version"],
            "latest_release_date": _first_upload(project.get("urls", [])),
            "stated_version": info["version"],
            "stated_version_exists": True,
            "stated_version_is_latest": True,
            "stated_version_yanked": bool(info.get("yanked")),
            "note": "Now run lookup_vulnerabilities and lookup_conda with this exact version.",
        }

    return await _guarded("pypi", work, timeout)


# ---------------------------------------------------------------------------- OSV


def _fix_for(vuln: dict[str, Any], name: str, version: Version) -> Version | None:
    """Smallest fixed version above ``version`` that this advisory lists for the package."""
    fixes: list[Version] = []
    for affected in vuln.get("affected", []):
        pkg = affected.get("package", {})
        if pkg.get("ecosystem") != "PyPI" or normalize_name(pkg.get("name", "")) != name:
            continue
        for rng in affected.get("ranges", []):
            if rng.get("type") != "ECOSYSTEM":
                continue
            for event in rng.get("events", []):
                fixed = parse_version(event["fixed"]) if "fixed" in event else None
                if fixed is not None and fixed > version:
                    fixes.append(fixed)
    return min(fixes) if fixes else None


def _severity(vuln: dict[str, Any]) -> str | None:
    sev = (vuln.get("database_specific") or {}).get("severity")
    return sev.upper() if isinstance(sev, str) and sev.upper() in _SEVERITY_ORDER else None


def _group_advisories(vulns: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group records describing the same issue (GHSA, PYSEC and CVE ids alias each other)."""
    groups: list[tuple[set[str], list[dict[str, Any]]]] = []
    for vuln in vulns:
        ids = {vuln["id"], *vuln.get("aliases", [])}
        merged_ids, merged = set(ids), [vuln]
        rest = []
        for g_ids, g_vulns in groups:
            if g_ids & ids:
                merged_ids |= g_ids
                merged = g_vulns + merged
            else:
                rest.append((g_ids, g_vulns))
        groups = rest + [(merged_ids, merged)]
    return [g for _, g in groups]


async def _osv_query(
    session: aiohttp.ClientSession, name: str, version: Version
) -> list[dict[str, Any]]:
    body: dict[str, Any] = {"package": {"name": name, "ecosystem": "PyPI"}, "version": str(version)}
    vulns: list[dict[str, Any]] = []
    while True:
        async with session.post(OSV_URL, json=body, headers={"User-Agent": USER_AGENT}) as resp:
            if resp.status != 200:
                raise _Unavailable(f"http {resp.status}")
            page = await resp.json(content_type=None)
        vulns.extend(page.get("vulns", []))
        token = page.get("next_page_token")
        if not token:
            break
        body["page_token"] = token
    return [v for v in vulns if not v.get("withdrawn")]


async def fetch_osv(
    session: aiohttp.ClientSession, package: str, version: str, *, timeout: float | None = None
) -> dict[str, Any]:
    """Known vulnerabilities affecting one version, and the first version that fixes them."""
    name = normalize_name(package)

    async def work() -> dict[str, Any]:
        parsed = parse_version(version)
        if parsed is None:
            # OSV matches loosely on unparseable versions, so never send one.
            return {"source": "osv", "status": "invalid_version", "package": name,
                    "note": f"'{version}' is not a valid version number. Ask the caller to repeat it."}  # fmt: skip
        vulns = await _osv_query(session, name, parsed)
        out: dict[str, Any] = {
            "source": "osv",
            "status": "ok",
            "package": name,
            "version": str(parsed),
            "vulnerable": bool(vulns),
            "vulnerability_count": 0,
        }
        if not vulns:
            out["note"] = (
                "OSV lists no known vulnerabilities for this exact version. If PyPI says the "
                "version does not exist, this result is meaningless."
            )
            return out

        advisories = []
        fixes: list[Version] = []
        unfixed = 0
        for group in _group_advisories(vulns):
            group_fixes = [f for v in group if (f := _fix_for(v, name, parsed)) is not None]
            fix = min(group_fixes) if group_fixes else None
            if fix is None:
                unfixed += 1
            else:
                fixes.append(fix)
            severities = [s for v in group if (s := _severity(v))]
            ids = sorted({i for v in group for i in (v["id"], *v.get("aliases", []))})
            summary = next((v["summary"] for v in group if v.get("summary")), None)
            advisories.append({
                "ids": ids,
                "summary": summary,
                "severity": max(severities, key=_SEVERITY_ORDER.index) if severities else None,
                "fixed_in": str(fix) if fix else None,
            })  # fmt: skip

        ranked = [a["severity"] for a in advisories if a["severity"]]
        advisories.sort(
            key=lambda a: _SEVERITY_ORDER.index(a["severity"]) if a["severity"] else -1,
            reverse=True,
        )
        out.update(
            vulnerability_count=len(advisories),
            highest_severity=max(ranked, key=_SEVERITY_ORDER.index) if ranked else None,
            without_a_fix=unfixed,
            advisories=advisories[:3],
        )
        if unfixed == 0 and fixes:
            # The smallest version at or above every individual fix.
            candidate = max(fixes)
            out["first_fixed_version"] = str(candidate)
            remaining = await _osv_query(session, name, candidate)
            out["first_fixed_version_is_clean"] = not remaining
            if remaining:
                out["note"] = (
                    f"{candidate} fixes everything affecting {parsed}, but OSV lists other "
                    f"vulnerabilities for {candidate} itself. Recommend upgrading past it and "
                    "offer to check a newer version."
                )
        else:
            out["first_fixed_version"] = None
            out["note"] = (
                f"{unfixed} of these have no fixed version listed in OSV. Do not name an "
                "upgrade target."
            )
        return out

    return await _guarded("osv", work, timeout)


# -------------------------------------------------------------------------- conda


def conda_name_candidates(package: str) -> list[str]:
    name = normalize_name(package)
    candidates = [CONDA_NAMES[name]] if name in CONDA_NAMES else []
    for candidate in (name, name.replace("-", "_"), name.replace("-", ".")):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


async def fetch_conda(
    session: aiohttp.ClientSession, package: str, version: str, *, timeout: float | None = None
) -> dict[str, Any]:
    """Which platforms the Anaconda defaults (main) channel has this exact version for."""
    name = normalize_name(package)

    async def work() -> dict[str, Any]:
        parsed = parse_version(version)
        if parsed is None:
            return {"source": "conda", "status": "invalid_version", "package": name,
                    "note": f"'{version}' is not a valid version number. Ask the caller to repeat it."}  # fmt: skip
        package_exists = False
        for conda_name in conda_name_candidates(name):
            status, payload = await _get_json(session, f"{CONDA_URL}/{conda_name}/{parsed}")
            if status == 200:
                break
            if "release version" in str(payload.get("error", "")):
                package_exists = True
                break
        else:
            return {
                "source": "conda", "status": "package_not_found", "package": name,
                "channel": "defaults",
                "note": "No package with this name was found on the Anaconda defaults channel. "
                "It may be published there under a different name, so say it was not found "
                "under this name rather than that it is unavailable.",
            }  # fmt: skip
        if package_exists:
            return {
                "source": "conda", "status": "version_not_found", "package": name,
                "conda_name": conda_name, "channel": "defaults", "version": str(parsed),
                "available": False,
                "note": f"The defaults channel carries {conda_name} but not version {parsed}. "
                "Which versions it does carry is unknown; do not guess.",
            }  # fmt: skip

        pythons: dict[str, set[tuple[int, int]]] = {}
        for dist in payload.get("distributions", []):
            subdir = (dist.get("attrs") or {}).get("subdir")
            if not subdir:
                continue
            builds = pythons.setdefault(subdir, set())
            m = _PY_BUILD.search((dist.get("attrs") or {}).get("build", ""))
            if m:
                builds.add((int(m.group(1)), int(m.group(2))))
        platforms = sorted(pythons)
        out: dict[str, Any] = {
            "source": "conda",
            "status": "ok",
            "package": name,
            "conda_name": conda_name,
            "channel": "defaults",
            "version": str(parsed),
            "available": bool(platforms),
            "platforms": platforms,
            "python_builds": {
                p: [f"{a}.{b}" for a, b in sorted(v)] for p, v in pythons.items() if v
            },
        }
        if "noarch" in platforms:
            out["note"] = "A noarch build installs on every platform, including ones not listed."
        else:
            out["note"] = "A platform that is not listed has no build of this version on defaults."
        return out

    return await _guarded("conda", work, timeout)
