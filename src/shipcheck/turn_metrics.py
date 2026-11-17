"""Per-turn latency recording.

Latency comes from the framework's per-turn metrics API: every ``ChatMessage``
added to the history carries a ``metrics`` dict (``MetricsReport``), delivered
through the ``conversation_item_added`` event. The deprecated session-level
``metrics_collected`` event is not used.

A turn starts at a user message and collects everything the agent does until
the next user message. Each finished turn is appended to a JSONL file for the
call and observed into Prometheus histograms.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from livekit.agents import AgentSession, ConversationItemAddedEvent
from livekit.agents.llm import ChatMessage
from prometheus_client import Counter, Histogram, values

logger = logging.getLogger("shipcheck.metrics")

_BUCKETS = (0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 10.0)


class _Metrics:
    """Prometheus collectors, created on first use.

    They are built lazily because the agent server empties the multiprocess
    directory when it starts; collectors created at import time in that process
    would be writing to deleted files.
    """

    def __init__(self) -> None:
        # prometheus_client chooses its storage when first imported. If something
        # imported it before PROMETHEUS_MULTIPROC_DIR was set (the lk CLI's console
        # and debugger runners do), switch to multiprocess storage now, since that
        # is what the agent server's /metrics endpoint reads.
        if "PROMETHEUS_MULTIPROC_DIR" in os.environ and values.ValueClass is values.MutexValue:
            values.ValueClass = values.get_value_class()
        # Label "kind" is "tool" or "no_tool"; "profile" names the tuning configuration.
        self.eot_delay = Histogram(
            "shipcheck_end_of_turn_delay_seconds",
            "End of caller speech to end-of-turn decision",
            ["kind", "profile"],
            buckets=_BUCKETS,
        )
        self.llm_ttft = Histogram(
            "shipcheck_llm_ttft_seconds",
            "LLM time to first token, per LLM response in the turn",
            ["kind", "profile"],
            buckets=_BUCKETS,
        )
        self.tts_ttfb = Histogram(
            "shipcheck_tts_ttfb_seconds",
            "TTS time to first byte, per spoken response in the turn",
            ["kind", "profile"],
            buckets=_BUCKETS,
        )
        self.first_audio = Histogram(
            "shipcheck_first_audio_seconds",
            "Caller finished speaking to first agent audio",
            ["kind", "profile"],
            buckets=_BUCKETS,
        )
        self.answer = Histogram(
            "shipcheck_answer_seconds",
            "Caller finished speaking to the start of the final answer",
            ["kind", "profile"],
            buckets=_BUCKETS,
        )
        self.tool_duration = Histogram(
            "shipcheck_tool_duration_seconds",
            "Duration of one tool call",
            ["tool", "status"],
            buckets=_BUCKETS,
        )
        self.tool_batch = Histogram(
            "shipcheck_tool_batch_seconds",
            "Wall time from first tool start to last tool end in a turn",
            ["profile"],
            buckets=_BUCKETS,
        )
        self.turns = Counter("shipcheck_turns_total", "Completed turns", ["kind", "profile"])
        self.upstream_failures = Counter(
            "shipcheck_upstream_failures_total",
            "Upstream lookups that failed or timed out",
            ["source"],
        )
        self.calls = Counter("shipcheck_calls_total", "Calls handled", ["profile"])


_metrics: _Metrics | None = None


def metrics() -> _Metrics:
    global _metrics
    if _metrics is None:
        _metrics = _Metrics()
    return _metrics


@dataclass
class ToolSpan:
    name: str
    started_at: float
    duration: float
    status: str


@dataclass
class Turn:
    call_id: str
    profile: str
    index: int
    started_at: float
    kind: str = "no_tool"
    user_stopped_speaking_at: float | None = None
    transcription_delay: float | None = None
    end_of_turn_delay: float | None = None
    # One entry per LLM response / spoken response, in order.
    llm_ttft: list[float] = field(default_factory=list)
    tts_ttfb: list[float] = field(default_factory=list)
    # Framework value from the first assistant message: end of speech to first agent audio.
    e2e_latency: float | None = None
    first_audio_latency: float | None = None
    answer_latency: float | None = None
    tool_duration: float | None = None
    # Caller finished speaking to the last tool finishing.
    tools_done_latency: float | None = None
    # How long the spoken "checking" line played, when there was one.
    filler_playout: float | None = None
    tools: list[ToolSpan] = field(default_factory=list)
    assistant_messages: int = 0
    interrupted: bool = False
    # False for typed turns (console --text, tests): they have no speech timings.
    audio: bool = False
    user_text: str | None = None
    agent_text: str | None = None


class TurnRecorder:
    """Collects per-turn metrics for one call."""

    def __init__(
        self, *, call_id: str, profile: str, out_dir: str | os.PathLike[str], log_text: bool = False
    ) -> None:
        self._call_id = call_id
        self._profile = profile
        self._log_text = log_text
        self._turn: Turn | None = None
        self._count = 0
        day = time.strftime("%Y%m%d")
        self.path = Path(out_dir) / f"{day}-{call_id}.jsonl"
        metrics().calls.labels(profile=profile).inc()

    def attach(self, session: AgentSession[Any]) -> None:
        session.on("conversation_item_added", self._on_item)
        session.on("close", lambda _ev: self.flush())

    # -- tool timing, fed by the tool wrapper in agent.py

    def record_tool(self, name: str, started_at: float, duration: float, status: str) -> None:
        span = ToolSpan(name=name, started_at=started_at, duration=duration, status=status)
        metrics().tool_duration.labels(tool=name, status=status).observe(duration)
        if self._turn is not None:
            self._turn.tools.append(span)
            self._turn.kind = "tool"

    # -- framework events

    def _on_item(self, ev: ConversationItemAddedEvent) -> None:
        item = ev.item
        if not isinstance(item, ChatMessage):
            return
        m: dict[str, Any] = dict(item.metrics or {})
        if item.role == "user":
            self.flush()
            self._count += 1
            self._turn = Turn(
                call_id=self._call_id,
                profile=self._profile,
                index=self._count,
                started_at=time.time(),
                user_stopped_speaking_at=m.get("stopped_speaking_at"),
                transcription_delay=m.get("transcription_delay"),
                end_of_turn_delay=m.get("end_of_turn_delay"),
                audio=m.get("stopped_speaking_at") is not None,
                user_text=item.text_content if self._log_text else None,
            )
        elif item.role == "assistant" and self._turn is not None:
            turn = self._turn
            turn.assistant_messages += 1
            turn.interrupted = turn.interrupted or bool(item.interrupted)
            if m.get("llm_node_ttft") is not None:
                turn.llm_ttft.append(m["llm_node_ttft"])
            if m.get("tts_node_ttfb") is not None:
                turn.tts_ttfb.append(m["tts_node_ttfb"])
            if turn.e2e_latency is None and m.get("e2e_latency") is not None:
                turn.e2e_latency = m["e2e_latency"]
            started = m.get("started_speaking_at")
            stopped = m.get("stopped_speaking_at")
            if turn.assistant_messages == 1 and started is not None and stopped is not None:
                # Provisional: only kept if a later message turns this one into a filler.
                turn.filler_playout = stopped - started
            if started is not None and turn.user_stopped_speaking_at is not None:
                latency = started - turn.user_stopped_speaking_at
                if turn.first_audio_latency is None:
                    turn.first_audio_latency = latency
                # The last assistant message of the turn is the answer.
                turn.answer_latency = latency
            if self._log_text:
                turn.agent_text = item.text_content

    def flush(self) -> None:
        """Finish the current turn: write it to JSONL and observe Prometheus."""
        turn, self._turn = self._turn, None
        if turn is None or turn.assistant_messages == 0:
            return
        if turn.tools:
            start = min(t.started_at for t in turn.tools)
            end = max(t.started_at + t.duration for t in turn.tools)
            turn.tool_duration = end - start
            if turn.user_stopped_speaking_at is not None:
                turn.tools_done_latency = end - turn.user_stopped_speaking_at
        if turn.assistant_messages < 2:
            turn.filler_playout = None
        # With no filler the framework value is the first-audio latency.
        if turn.first_audio_latency is None:
            turn.first_audio_latency = turn.e2e_latency

        labels = {"kind": turn.kind, "profile": turn.profile}
        metrics().turns.labels(**labels).inc()
        if turn.end_of_turn_delay is not None:
            metrics().eot_delay.labels(**labels).observe(turn.end_of_turn_delay)
        for v in turn.llm_ttft:
            metrics().llm_ttft.labels(**labels).observe(v)
        for v in turn.tts_ttfb:
            metrics().tts_ttfb.labels(**labels).observe(v)
        if turn.first_audio_latency is not None:
            metrics().first_audio.labels(**labels).observe(turn.first_audio_latency)
        if turn.answer_latency is not None:
            metrics().answer.labels(**labels).observe(turn.answer_latency)
        if turn.tool_duration is not None:
            metrics().tool_batch.labels(profile=turn.profile).observe(turn.tool_duration)

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(asdict(turn), separators=(",", ":")) + "\n")
        except OSError as e:
            logger.warning("could not write turn metrics to %s: %s", self.path, e)
        logger.info(
            "turn %d kind=%s eot=%s llm_ttft=%s tts_ttfb=%s tools=%s first_audio=%s answer=%s",
            turn.index, turn.kind, turn.end_of_turn_delay, turn.llm_ttft, turn.tts_ttfb,
            turn.tool_duration, turn.first_audio_latency, turn.answer_latency,
        )  # fmt: skip
