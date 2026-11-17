"""Shipcheck: an inbound voice agent that says whether a Python package version is safe to ship."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import aiohttp
from livekit import agents
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    RunContext,
    TurnHandlingOptions,
    UserStateChangedEvent,
    function_tool,
    inference,
    utils,
)

from . import config, names, upstreams
from .turn_metrics import TurnRecorder, metrics

logger = logging.getLogger("shipcheck")
settings = config.settings

GREETING = "Shipcheck here. Which Python package and version are you thinking of shipping?"
NUDGE = "Are you still there? Tell me a package name and a version and I'll check it."
GOODBYE = "I haven't heard anything, so I'll hang up now. Goodbye."

INSTRUCTIONS = """\
You are Shipcheck, a voice agent on a phone line. A developer calls to ask whether a specific \
version of a Python package is safe to ship. Today is {today}.

# How you speak
- This is a phone call. Use plain spoken English: no lists, no markdown, no symbols, no emoji.
- Answer in two or three short sentences. Give the verdict first, then the reason.
- In the reason, name the package the tools matched and the version, so the caller can \
catch a misheard name.
- Say version numbers the way people do: "two point one point zero".
- Never read out CVE, GHSA or PYSEC identifiers unless the caller asks for them.
- Do not say you are about to look something up. The phone system already says a short \
"checking" line while tools run.

# What you need
A package name and a version. If either is missing, ask for it in one short question. If the \
caller says "latest", call lookup_pypi with version "latest" first, then run the other two \
lookups with the version it returns.

# Looking things up
When you have a name and a version, call lookup_pypi, lookup_vulnerabilities and lookup_conda \
together in the same step, with the same package and version. Pass the package name as you \
heard it; the tools match it to a real PyPI project and return the matched name in "package".
- If a tool returns status "name_not_resolved", nothing was looked up, so give no verdict. \
With candidates, ask \
"did you mean X?". With none, ask the caller to spell the name letter by letter, then call \
confirm_spelled_name with exactly what they spelled, and run the three lookups with the name \
it returns.
- For a follow-up about the same package and version, such as a platform question, answer \
from the results you already have. Only look up again for a different package or version.

# The verdict
Start your answer with exactly one verdict: "Ship it", "Upgrade to" plus a version, or \
"Hold". Never combine two. Use the first rule below that applies.
1. Hold, if PyPI says the version does not exist, the package was not found, or the version \
was yanked. Say which.
2. Hold, if the vulnerability lookup is unavailable. Say you could not check vulnerabilities \
right now, so you cannot clear it.
3. Upgrade to first_fixed_version, if the version is vulnerable and first_fixed_version is \
set. Mention how many known vulnerabilities there are and the highest severity.
4. Hold, if the version is vulnerable and there is no first_fixed_version.
5. Ship, if there are no known vulnerabilities. If it is not the latest version, mention the \
latest version in passing.
Mention conda only briefly unless asked: whether the defaults channel has this version. If \
lookup_conda returned version_not_found, no platform has that version on defaults, so a \
platform question gets a plain "no". For \
platform questions, map what the caller says to conda platform names: aarch64 or ARM Linux is \
linux-aarch64, Apple silicon or M1 is osx-arm64, Intel Mac is osx-64, Windows is win-64, \
Linux is linux-64. A noarch build works on every platform.

# Never guess
Every version number, date, count and severity you say must come from a tool result in this \
call. If a tool returned status "unavailable", say that source could not be reached and that \
you do not know; never fill the gap from memory. If asked something the tools did not return, \
say you do not have that information. You only answer questions about Python packages.
"""


@dataclass
class CallState:
    """Per-call state, available to tools as ``ctx.userdata``."""

    recorder: TurnRecorder | None = None
    # Names the caller spelled out; they resolve as exact even if not in the local list.
    confirmed_names: set[str] = field(default_factory=set)
    # Speech handles that already got a "checking" line, so parallel tools say it once.
    filler_said: set[str] = field(default_factory=set)
    speak_filler: bool = True


@contextlib.asynccontextmanager
async def _http_session() -> AsyncIterator[aiohttp.ClientSession]:
    """The job's shared aiohttp session, or a temporary one outside a job (tests, scripts)."""
    try:
        yield utils.http_context.http_session()
    except RuntimeError:
        async with aiohttp.ClientSession() as session:
            yield session


def _spoken_version(version: str) -> str:
    return version.replace(".", " point ")


class ShipcheckAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=INSTRUCTIONS.format(today=date.today().strftime("%d %B %Y")))

    async def on_enter(self) -> None:
        self.session.say(GREETING)

    # ------------------------------------------------------------------ helpers

    def _resolve(self, ctx: RunContext[CallState], package: str) -> tuple[str | None, str | None]:
        """Return (name, None) for a resolved project or (None, tool_result_json)."""
        res = names.resolve(package, confirmed=ctx.userdata.confirmed_names)
        if res.resolved:
            return res.name, None
        result: dict[str, Any] = {
            "status": "name_not_resolved",
            "heard": package,
            "candidates": [c.name for c in res.candidates],
        }
        if res.candidates:
            result["note"] = (
                "Not sure which project the caller means. Ask whether they meant the first "
                "candidate; if they say no, ask them to spell the name."
            )
        else:
            result["note"] = (
                "This does not match any well-known PyPI project. Ask the caller to spell the "
                "name letter by letter, then call confirm_spelled_name."
            )
        return None, json.dumps(result)

    async def _lookup(
        self,
        ctx: RunContext[CallState],
        tool: str,
        package: str,
        version: str,
        fetch: Callable[[aiohttp.ClientSession, str, str], Awaitable[dict[str, Any]]],
    ) -> str:
        """Shared body of the three lookup tools: resolve, speak a filler, fetch, time it."""
        started_wall = time.time()
        started = time.perf_counter()
        status = "name_not_resolved"
        try:
            name, unresolved = self._resolve(ctx, package)
            if name is None:
                return unresolved or ""

            def filler(_step: int) -> str | None:
                # The three lookups run in parallel; only the first one speaks.
                state = ctx.userdata
                if not state.speak_filler or ctx.speech_handle.id in state.filler_said:
                    return None
                state.filler_said.add(ctx.speech_handle.id)
                if version.strip().lower() == "latest":
                    return f"Checking the latest {name}."
                return f"Checking {name} {_spoken_version(version)}."

            async with (
                ctx.with_filler(filler, delay=settings.filler_delay),
                _http_session() as http,
            ):
                result = await fetch(http, name, version)
            status = result["status"]
            if status == "unavailable":
                metrics().upstream_failures.labels(source=result["source"]).inc()
            return json.dumps(result)
        finally:
            if ctx.userdata.recorder is not None:
                ctx.userdata.recorder.record_tool(
                    tool, started_wall, time.perf_counter() - started, status
                )

    # -------------------------------------------------------------------- tools

    @function_tool()
    async def lookup_pypi(self, ctx: RunContext[CallState], package: str, version: str) -> str:
        """Look up a package on PyPI: whether the stated version exists, its release date,
        whether it was yanked, and the latest version and its release date.

        Call together with lookup_vulnerabilities and lookup_conda.

        Args:
            package: The package name as the caller said it, for example "pie torch".
            version: The version the caller wants to ship, for example "2.1.0", or "latest".
        """

        async def fetch(http: aiohttp.ClientSession, name: str, ver: str) -> dict[str, Any]:
            if ver.strip().lower() != "latest":
                return await upstreams.fetch_pypi(http, name, ver)
            return await upstreams.fetch_pypi_latest(http, name)

        return await self._lookup(ctx, "lookup_pypi", package, version, fetch)

    @function_tool()
    async def lookup_vulnerabilities(
        self, ctx: RunContext[CallState], package: str, version: str
    ) -> str:
        """Look up known vulnerabilities affecting one exact version of a package in the OSV
        database, and the first version that fixes all of them.

        Call together with lookup_pypi and lookup_conda.

        Args:
            package: The package name as the caller said it.
            version: The exact version to check, for example "2.1.0".
        """
        return await self._lookup(
            ctx, "lookup_vulnerabilities", package, version, upstreams.fetch_osv
        )

    @function_tool()
    async def lookup_conda(self, ctx: RunContext[CallState], package: str, version: str) -> str:
        """Look up which platforms the Anaconda defaults channel has builds of one exact
        version for, such as linux-64, linux-aarch64, osx-arm64 and win-64.

        Call together with lookup_pypi and lookup_vulnerabilities.

        Args:
            package: The package name as the caller said it.
            version: The exact version to check, for example "2.1.0".
        """
        return await self._lookup(ctx, "lookup_conda", package, version, upstreams.fetch_conda)

    @function_tool()
    async def confirm_spelled_name(self, ctx: RunContext[CallState], spelling: str) -> str:
        """Record a package name the caller spelled out letter by letter.

        Use only after asking the caller to spell the name.

        Args:
            spelling: The letters exactly as the caller said them, for example "t o r c h".
        """
        name = names.parse_spelling(spelling)
        if name is None:
            return json.dumps({
                "status": "not_understood",
                "note": "That did not spell a valid package name. Ask the caller to spell it "
                "once more, slowly.",
            })  # fmt: skip
        ctx.userdata.confirmed_names.add(name)
        return json.dumps({
            "status": "ok",
            "package": name,
            "note": "Read the name back in one short phrase, then run the three lookups with "
            "this exact name.",
        })  # fmt: skip


def build_session(state: CallState) -> AgentSession[CallState]:
    endpointing: dict[str, Any] = {}
    if settings.min_endpointing_delay is not None:
        endpointing["min_delay"] = settings.min_endpointing_delay
    if settings.max_endpointing_delay is not None:
        endpointing["max_delay"] = settings.max_endpointing_delay
    turn_handling = TurnHandlingOptions(
        turn_detection=inference.TurnDetector(),
        preemptive_generation={"preemptive_tts": settings.preemptive_tts},
    )
    if endpointing:
        turn_handling["endpointing"] = endpointing  # type: ignore[typeddict-item]
    return AgentSession[CallState](
        stt=inference.STT(model=settings.stt_model, language="en"),
        llm=inference.LLM(model=settings.llm_model),
        tts=inference.TTS(model=settings.tts_model, voice=settings.tts_voice),
        turn_handling=turn_handling,
        user_away_timeout=settings.user_away_timeout,
        userdata=state,
    )


def _handle_silence(session: AgentSession[CallState]) -> None:
    """Nudge a silent caller once, then say goodbye and end the call."""
    task: asyncio.Task[None] | None = None

    async def nudge_then_hang_up() -> None:
        await session.say(NUDGE)
        await asyncio.sleep(settings.hangup_after_nudge)
        await session.say(GOODBYE, allow_interruptions=False)
        session.shutdown()

    @session.on("user_state_changed")
    def _on_user_state(ev: UserStateChangedEvent) -> None:
        nonlocal task
        if ev.new_state == "away":
            task = asyncio.create_task(nudge_then_hang_up())
        elif task is not None:
            task.cancel()
            task = None


server = AgentServer(prometheus_port=settings.prometheus_port)


@server.rtc_session(agent_name=settings.agent_name)
async def entrypoint(ctx: JobContext) -> None:
    ctx.log_context_fields = {"room": ctx.room.name}
    recorder = TurnRecorder(
        call_id=f"{ctx.room.name}-{ctx.job.id}",
        profile=settings.profile,
        out_dir=settings.turns_dir,
        log_text=settings.log_text,
    )
    state = CallState(recorder=recorder)
    session = build_session(state)
    recorder.attach(session)
    _handle_silence(session)
    ctx.add_shutdown_callback(_flush(recorder))

    await session.start(room=ctx.room, agent=ShipcheckAgent())


def _flush(recorder: TurnRecorder) -> Callable[[], Awaitable[None]]:
    async def flush() -> None:
        recorder.flush()
        logger.info("turn metrics written to %s", recorder.path)

    return flush


def main() -> None:
    agents.cli.run_app(server)


if __name__ == "__main__":
    main()
