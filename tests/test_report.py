"""The report script's statistics."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "report", Path(__file__).resolve().parent.parent / "scripts" / "report.py"
)
assert spec and spec.loader
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def test_percentiles() -> None:
    vals = [0.1, 0.2, 0.3, 0.4, 1.0]
    assert report.percentile(vals, 0.5) == 0.3
    assert abs(report.percentile(vals, 0.95) - 0.88) < 1e-9
    assert report.percentile([0.7], 0.95) == 0.7


def test_tables_split_tool_and_no_tool_turns(tmp_path: Path) -> None:
    turns = [
        {
            "call_id": "a",
            "profile": "baseline",
            "kind": "no_tool",
            "end_of_turn_delay": 0.5,
            "llm_ttft": [0.3],
            "tts_ttfb": [0.2],
            "first_audio_latency": 1.0,
            "answer_latency": 1.0,
            "tool_duration": None,
        },
        {
            "call_id": "a",
            "profile": "baseline",
            "kind": "tool",
            "end_of_turn_delay": 0.6,
            "llm_ttft": [0.3, 0.5],
            "tts_ttfb": [0.2, 0.2],
            "first_audio_latency": 1.2,
            "answer_latency": 3.0,
            "tool_duration": 0.7,
        },
        {
            "call_id": "b",
            "profile": "tuned",
            "kind": "tool",
            "end_of_turn_delay": 0.3,
            "llm_ttft": [0.3, 0.5],
            "tts_ttfb": [0.2, 0.2],
            "first_audio_latency": 0.9,
            "answer_latency": 2.6,
            "tool_duration": 0.7,
        },
    ]
    (tmp_path / "x.jsonl").write_text("\n".join(json.dumps(t) for t in turns))
    loaded = report.by_profile(report.load_turns(tmp_path))
    table = report.latency_table(loaded["baseline"], "baseline")
    assert "No-tool turns (n=1)" in table and "Tool turns (n=1)" in table
    assert "| Tool duration (parallel batch) | n/a | 700 ms / 700 ms |" in table
    assert "| LLM time to first token | 300 ms / 300 ms | 400 ms / 490 ms |" in table
    cmp = report.compare_table(loaded["baseline"], loaded["tuned"], "baseline", "tuned")
    assert "| Tool turns | End-of-turn delay | 600 ms / 600 ms | 300 ms / 300 ms | −300 ms |" in cmp


def test_readme_block_replacement() -> None:
    text = "intro\n<!-- latency:start -->\nold\n<!-- latency:end -->\noutro"
    out = report.replace_block(text, "latency", "new table")
    assert out == "intro\n<!-- latency:start -->\nnew table\n<!-- latency:end -->\noutro"
