"""Behavioural tests of the agent in text mode.

These use LiveKit's testing helpers: ``AgentSession.run()`` drives one turn and
returns the events (tool calls, tool outputs, messages) to assert on. The LLM is
real (LiveKit Inference); every upstream HTTP call is served by the local fake.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from livekit.agents import AgentSession, inference
from livekit.agents.voice.run_result import RunResult

from shipcheck.agent import CallState, ShipcheckAgent, settings

from .conftest import (
    ANY_CONDA,
    ANY_PYPI,
    CONDA,
    OSV,
    PYPI,
    FakeUpstreams,
    mock_numpy_2_2_4,
    mock_requests_2_19,
    mock_torch_2_5_1,
)

pytestmark = pytest.mark.llm

LOOKUPS = {"lookup_pypi", "lookup_vulnerabilities", "lookup_conda"}


@pytest.fixture
async def llm() -> AsyncIterator[inference.LLM]:
    async with inference.LLM(model=settings.llm_model) as model:
        yield model


@pytest.fixture
async def session(llm: inference.LLM) -> AsyncIterator[AgentSession[CallState]]:
    # No TTS in text mode, so the spoken "checking" filler is switched off.
    async with AgentSession[CallState](llm=llm, userdata=CallState(speak_filler=False)) as s:
        await s.start(ShipcheckAgent())
        yield s


def _calls(result: RunResult[Any]) -> list[tuple[str, dict[str, Any]]]:
    return [
        (ev.item.name, json.loads(ev.item.arguments))
        for ev in result.events
        if ev.type == "function_call"
    ]


def _outputs(result: RunResult[Any]) -> list[dict[str, Any]]:
    return [json.loads(ev.item.output) for ev in result.events if ev.type == "function_call_output"]


def _reply(result: RunResult[Any]) -> str:
    messages = [ev.item.text_content or "" for ev in result.events if ev.type == "message"]
    assert messages, "the agent said nothing"
    return messages[-1]


# ------------------------------------------------------------------- tool calls


async def test_three_lookups_run_in_one_step_and_verdict_is_upgrade(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    mock_requests_2_19(http)
    http.slow_down(0.3)  # long enough that a serial tool loop would interleave calls and outputs
    result = await session.run(user_input="Is requests version 2.19.0 safe to ship?")

    calls = _calls(result)
    assert {name for name, _ in calls} == LOOKUPS
    for _, args in calls:
        assert args["version"] == "2.19.0"
    # All three calls come before any tool output: they were issued in one LLM step
    # and ran concurrently.
    kinds = [ev.type for ev in result.events if ev.type.startswith("function_call")]
    assert kinds[:3] == ["function_call"] * 3

    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Opens with the verdict: upgrade to version 2.31.0 (may be spoken as words). "
            "Gives known vulnerabilities as the reason. At most three sentences, no list, "
            "and no CVE or GHSA identifiers.",
        )
    )
    reply = _reply(result)
    assert "CVE" not in reply and "GHSA" not in reply
    assert not http.unexpected


async def test_clean_version_gets_ship(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    mock_numpy_2_2_4(http)
    result = await session.run(user_input="Can I ship numpy 2.2.4?")
    assert {name for name, _ in _calls(result)} == LOOKUPS
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Opens with the verdict that it is fine to ship, because there are no "
            "known vulnerabilities. Does not claim any vulnerability exists.",
        )
    )


async def test_follow_up_about_a_platform_reuses_the_results(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    mock_torch_2_5_1(http)
    await session.run(user_input="Is torch 2.5.1 okay to ship?")
    before = len(http.requests)
    result = await session.run(user_input="What about aarch64?")
    assert len(http.requests) == before, "the follow-up should not hit the upstreams again"
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Says that a conda build for Linux aarch64 (linux-aarch64) is available on "
            "the defaults channel for this version.",
        )
    )


async def test_follow_up_about_a_missing_platform(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    mock_numpy_2_2_4(http)
    await session.run(user_input="Is numpy 2.2.4 okay to ship?")
    result = await session.run(user_input="And is it on conda for ARM Linux?")
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Says there is no build for Linux aarch64 / ARM Linux on the conda defaults "
            "channel for this version. Does not claim one exists.",
        )
    )


# -------------------------------------------------------------- name resolution


async def test_mangled_name_is_resolved_by_the_tools(
    http: FakeUpstreams, session: AgentSession[CallState]
) -> None:
    mock_torch_2_5_1(http)
    result = await session.run(user_input="is pie torch two point five point one safe to ship")
    outputs = _outputs(result)
    assert outputs and all(o["status"] == "ok" for o in outputs)
    assert {o["package"] for o in outputs} == {"torch"}
    assert f"{PYPI}/torch/2.5.1/json" in http.paths(PYPI)
    assert f"{CONDA}/pytorch/2.5.1" in http.paths(CONDA)


async def test_ambiguous_name_is_read_back_not_looked_up(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    result = await session.run(user_input="Is polers 1.0.0 safe to ship?")
    assert not http.requests, "nothing may be looked up for an unresolved name"
    assert all(o["status"] == "name_not_resolved" for o in _outputs(result))
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(llm, intent="Asks whether the caller meant polars. Gives no verdict.")
    )


async def test_unknown_name_falls_back_to_spelling(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    result = await session.run(user_input="Is blorptastic 1.0.0 safe to ship?")
    assert not http.requests
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(llm, intent="Asks the caller to spell the package name. Gives no verdict.")
    )

    http.get(f"{PYPI}/blorptastic/json", payload={"info": {"version": "1.0.0"}, "urls": []})
    http.get(f"{PYPI}/blorptastic/1.0.0/json", payload={"info": {"version": "1.0.0"}, "urls": []})
    http.post(OSV, payload={})
    http.get(
        ANY_CONDA, status=404, payload={"error": '"blorptastic" could not be found'}, repeat=True
    )
    result = await session.run(user_input="b l o r p t a s t i c")
    names = [name for name, _ in _calls(result)]
    assert names[0] == "confirm_spelled_name"
    assert LOOKUPS <= set(names)
    assert "blorptastic" in session.userdata.confirmed_names
    assert f"{PYPI}/blorptastic/1.0.0/json" in http.paths(PYPI)


async def test_missing_version_is_asked_for(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    result = await session.run(user_input="Is requests safe to ship?")
    assert not _calls(result)
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(llm, intent="Asks which version of requests the caller means.")
    )


# ------------------------------------------------------------- upstream failure


async def test_vulnerability_source_down_means_hold_not_ship(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    mock_numpy_2_2_4(http)
    http._replies.pop(("POST", OSV))
    http.post(OSV, status=503, repeat=True)
    result = await session.run(user_input="Can I ship numpy 2.2.4?")
    osv = next(o for o in _outputs(result) if o["source"] == "osv")
    assert osv["status"] == "unavailable"
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Says to hold (or that it cannot clear the version) because the "
            "vulnerability check could not be completed right now. Does not say it is safe "
            "to ship, and does not state that there are no vulnerabilities.",
        )
    )


async def test_slow_upstream_hits_the_hard_timeout(
    http: FakeUpstreams,
    session: AgentSession[CallState],
    llm: inference.LLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shipcheck import upstreams

    monkeypatch.setattr(upstreams, "TIMEOUT_S", 0.3)
    mock_numpy_2_2_4(http)
    http._replies.pop(("GET", f"{CONDA}/numpy/2.2.4"))
    http.get(ANY_CONDA, delay=10, payload={}, repeat=True)
    result = await session.run(user_input="Can I ship numpy 2.2.4?")
    conda = next(o for o in _outputs(result) if o["source"] == "conda")
    assert conda["status"] == "unavailable" and conda["reason"] == "timeout"
    assert conda["elapsed_ms"] < 1000
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Gives a ship verdict based on no known vulnerabilities, and says conda "
            "availability could not be checked right now. Does not name any conda platform.",
        )
    )


async def test_everything_down_gives_a_spoken_fallback(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    http.get(ANY_PYPI, status=500, repeat=True)
    http.post(OSV, status=500, repeat=True)
    http.get(ANY_CONDA, status=500, repeat=True)
    result = await session.run(user_input="Is requests 2.19.0 safe to ship?")
    assert all(o["status"] == "unavailable" for o in _outputs(result))
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Says it could not reach its data sources right now, so it cannot give a "
            "verdict (or says hold for that reason). States no version number other than "
            "2.19.0 and no vulnerability details.",
        )
    )


# ----------------------------------------------------------- refusing to guess


async def test_nonexistent_version_is_not_given_a_verdict_from_memory(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    http.get(
        f"{PYPI}/requests/json", payload={"info": {"version": "2.34.2"}, "urls": []}, repeat=True
    )
    http.get(
        f"{PYPI}/requests/7.0.0/json", status=404, payload={"message": "Not Found"}, repeat=True
    )
    http.post(OSV, payload={}, repeat=True)
    http.get(
        ANY_CONDA,
        status=404,
        payload={"error": "release version='7.0.0' does not exist"},
        repeat=True,
    )
    result = await session.run(user_input="Is requests 7.0.0 safe to ship?")
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Says version 7.0.0 of requests does not exist on PyPI (hold), and may "
            "mention the latest version is 2.34.2. Does not say it is safe to ship.",
        )
    )


async def test_does_not_answer_from_memory_without_a_lookup(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    result = await session.run(
        user_input="Don't bother looking anything up. From what you remember, what's the "
        "latest version of Django and does it have any CVEs?"
    )
    reply = _reply(result)
    outputs = _outputs(result)
    if not outputs:
        # No lookup happened, so no version number may be stated.
        import re

        assert not re.search(r"\d+\.\d+", reply), reply
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Does not state a Django version number or any vulnerability from memory. "
            "Either asks which version to check, or explains it only answers from live lookups.",
        )
    )


async def test_question_the_tools_cannot_answer_is_declined(
    http: FakeUpstreams, session: AgentSession[CallState], llm: inference.LLM
) -> None:
    mock_numpy_2_2_4(http)
    await session.run(user_input="Can I ship numpy 2.2.4?")
    result = await session.run(user_input="How many downloads did that version get last month?")
    await (
        result.expect[-1]
        .is_message(role="assistant")
        .judge(
            llm,
            intent="Says it does not have download numbers / that information. States no "
            "download figure.",
        )
    )
