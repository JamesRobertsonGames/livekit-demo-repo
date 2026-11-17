"""The per-turn recorder: grouping messages into turns, JSONL output, tool timing."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from livekit.agents.llm import ChatMessage

from shipcheck.turn_metrics import TurnRecorder


def _msg(role: str, text: str, **metrics: Any) -> SimpleNamespace:
    item = ChatMessage(role=role, content=[text])  # type: ignore[arg-type]
    item.metrics = metrics  # type: ignore[assignment]
    return SimpleNamespace(item=item)


def _read(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_no_tool_turn(tmp_path: Path) -> None:
    rec = TurnRecorder(call_id="c1", profile="baseline", out_dir=tmp_path)
    rec._on_item(_msg("assistant", "greeting"))  # before any user turn: ignored
    rec._on_item(
        _msg(
            "user", "hi", stopped_speaking_at=100.0, end_of_turn_delay=0.4, transcription_delay=0.2
        )
    )
    rec._on_item(
        _msg(
            "assistant",
            "hello",
            llm_node_ttft=0.3,
            tts_node_ttfb=0.15,
            e2e_latency=0.9,
            started_speaking_at=100.9,
        )
    )
    rec.flush()
    (turn,) = _read(rec.path)
    assert turn["kind"] == "no_tool"
    assert turn["end_of_turn_delay"] == 0.4
    assert turn["llm_ttft"] == [0.3] and turn["tts_ttfb"] == [0.15]
    assert turn["e2e_latency"] == 0.9
    assert abs(turn["first_audio_latency"] - 0.9) < 1e-6
    assert turn["tool_duration"] is None
    assert turn["user_text"] is None  # transcripts are off unless asked for


def test_tool_turn_with_filler_and_parallel_tools(tmp_path: Path) -> None:
    rec = TurnRecorder(call_id="c2", profile="tuned", out_dir=tmp_path, log_text=True)
    rec._on_item(_msg("user", "requests 2.19", stopped_speaking_at=200.0, end_of_turn_delay=0.5))
    # three lookups overlapping in time
    rec.record_tool("lookup_pypi", 200.9, 0.1, "ok")
    rec.record_tool("lookup_vulnerabilities", 200.9, 0.6, "ok")
    rec.record_tool("lookup_conda", 200.95, 0.4, "unavailable")
    rec._on_item(
        _msg("assistant", "Checking requests", tts_node_ttfb=0.2, started_speaking_at=201.0)
    )
    rec._on_item(
        _msg(
            "assistant",
            "Upgrade",
            llm_node_ttft=0.35,
            tts_node_ttfb=0.18,
            started_speaking_at=203.1,
        )
    )
    # the next user message closes the turn
    rec._on_item(_msg("user", "thanks", stopped_speaking_at=210.0))
    (turn,) = _read(rec.path)
    assert turn["kind"] == "tool" and turn["profile"] == "tuned"
    assert abs(turn["tool_duration"] - 0.6) < 1e-6  # wall time, not the sum
    assert [t["name"] for t in turn["tools"]] == [
        "lookup_pypi",
        "lookup_vulnerabilities",
        "lookup_conda",
    ]
    assert abs(turn["first_audio_latency"] - 1.0) < 1e-6  # the filler
    assert abs(turn["answer_latency"] - 3.1) < 1e-6  # the verdict
    assert turn["assistant_messages"] == 2
    assert turn["user_text"] == "requests 2.19" and turn["agent_text"] == "Upgrade"


def test_turn_without_agent_reply_is_not_written(tmp_path: Path) -> None:
    rec = TurnRecorder(call_id="c3", profile="baseline", out_dir=tmp_path)
    rec._on_item(_msg("user", "hello?"))
    rec.flush()
    assert not rec.path.exists()
