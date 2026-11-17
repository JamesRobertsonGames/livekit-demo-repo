"""Environment-driven settings. Imported before anything from livekit or prometheus."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env.local")


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _opt_float(name: str) -> float | None:
    raw = os.environ.get(name)
    return float(raw) if raw else None


@dataclass(frozen=True)
class Settings:
    agent_name: str = os.environ.get("SHIPCHECK_AGENT_NAME", "shipcheck")
    # Models are LiveKit Inference ids, so only LiveKit credentials are needed.
    stt_model: str = os.environ.get("SHIPCHECK_STT_MODEL", "assemblyai/universal-3-6-pro")
    llm_model: str = os.environ.get("SHIPCHECK_LLM_MODEL", "google/gemma-4-31b-it")
    tts_model: str = os.environ.get("SHIPCHECK_TTS_MODEL", "fishaudio/s2.1-pro")
    tts_voice: str = os.environ.get("SHIPCHECK_TTS_VOICE", "fa4c9eb3dccc4806b382b40d61c6b10a")

    # Turn-taking. None keeps the framework default.
    min_endpointing_delay: float | None = _opt_float("SHIPCHECK_MIN_ENDPOINTING_DELAY")
    max_endpointing_delay: float | None = _opt_float("SHIPCHECK_MAX_ENDPOINTING_DELAY")
    # Seconds the lookups must have been running before the spoken "checking" line
    # starts. 0 always says it; a larger value skips it when the lookups are quick.
    filler_delay: float = _float("SHIPCHECK_FILLER_DELAY", 0.0)
    preemptive_tts: bool = os.environ.get("SHIPCHECK_PREEMPTIVE_TTS", "0") == "1"

    # Silence handling: nudge after this many seconds, hang up after the second nudge.
    user_away_timeout: float = _float("SHIPCHECK_USER_AWAY_TIMEOUT", 10.0)
    hangup_after_nudge: float = _float("SHIPCHECK_HANGUP_AFTER_NUDGE", 10.0)

    # Measurement.
    profile: str = os.environ.get("SHIPCHECK_PROFILE", "baseline")
    turns_dir: Path = Path(os.environ.get("SHIPCHECK_TURNS_DIR", "data/turns"))
    log_text: bool = os.environ.get("SHIPCHECK_LOG_TEXT", "0") == "1"
    prometheus_port: int | None = (
        int(os.environ["SHIPCHECK_PROMETHEUS_PORT"])
        if os.environ.get("SHIPCHECK_PROMETHEUS_PORT")
        else None
    )

    # Stretch: warm transfer target.
    transfer_to: str | None = os.environ.get("SHIPCHECK_TRANSFER_TO") or None
    sip_outbound_trunk_id: str | None = os.environ.get("SHIPCHECK_SIP_OUTBOUND_TRUNK_ID") or None


settings = Settings()

# prometheus_client picks its storage backend when it is first imported, and job
# processes are separate from the process serving /metrics, so multiprocess mode
# has to be switched on here, before livekit.agents pulls prometheus_client in.
if settings.prometheus_port is not None and "PROMETHEUS_MULTIPROC_DIR" not in os.environ:
    _dir = Path(tempfile.gettempdir()) / "shipcheck-prometheus"
    _dir.mkdir(parents=True, exist_ok=True)
    os.environ["PROMETHEUS_MULTIPROC_DIR"] = str(_dir)
