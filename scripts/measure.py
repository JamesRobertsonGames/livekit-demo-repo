"""Collect latency data by speaking scripted calls into the agent's full audio pipeline.

Uses `lk agent debugger --audio`: each caller line is synthesised and fed to the
agent's microphone input, so speech-to-text, turn detection, the LLM, the tools
(live PyPI, OSV and Anaconda) and text-to-speech all run for real. Each scripted
call is a separate session, and the agent writes its per-turn JSONL as usual.

    uv run scripts/measure.py --profile baseline
    SHIPCHECK_FILLER_DELAY=0.8 uv run scripts/measure.py --profile tuned

This costs LiveKit Inference usage (STT, LLM, TTS) on your project.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

# Each inner list is one call. Lines are written the way a caller would say them.
CALLS: list[list[str]] = [
    [
        "Is requests version two point one nine point zero safe to ship?",
        "What about Linux on ARM sixty four?",
        "What's the latest version?",
        "When was two point one nine point zero released?",
        "Okay. Can I ship numpy two point two point four?",
        "Is that on conda for Windows?",
        "Which Python versions is it built for on Linux?",
        "Great, thanks.",
    ],
    [
        "Is pie torch two point five point one okay to ship?",
        "Is there a conda build for Apple silicon?",
        "How severe is the worst one?",
        "Is flask three point zero point zero safe to ship?",
        "And what about Linux ARM?",
        "What about jango four point two point zero?",
        "What's the newest jango release?",
        "Thank you.",
    ],
    [
        "I want to ship pandas two point two point zero.",
        "What's the newest release?",
        "Check sci kit learn one point four point zero.",
        "Is it on the defaults channel for Windows?",
        "And fast A P I zero point one one zero point zero?",
        "When did that version come out?",
        "What about pillow nine point zero point zero?",
        "How many vulnerabilities was that?",
    ],
    [
        "Can I ship U R L lib three version one point two six point five?",
        "What should I upgrade to?",
        "Is cryptography forty one point zero point zero safe?",
        "Is that one on conda for Intel Macs?",
        "And jinja two version three point one point two?",
        "What's the highest severity there?",
        "Is requests safe to ship?",
        "Never mind, thanks.",
    ],
]


def lk(*args: str, timeout: float = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["lk", "agent", "debugger", *args], capture_output=True, text=True, timeout=timeout
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--profile", required=True, help="label written to every turn, e.g. baseline or tuned"
    )
    parser.add_argument(
        "--calls", type=int, default=len(CALLS), help="how many of the scripted calls to run"
    )
    parser.add_argument("--repeat", type=int, default=1, help="run the call set this many times")
    args = parser.parse_args()

    os.environ["SHIPCHECK_PROFILE"] = args.profile
    turns = 0
    for round_no in range(args.repeat):
        for call_no, lines in enumerate(CALLS[: args.calls], start=1):
            lk("stop")
            started = lk("start", "--audio")
            if started.returncode != 0:
                sys.exit(f"could not start the agent:\n{started.stdout}\n{started.stderr}")
            for line in lines:
                result = lk("say", "--metrics", line)
                turns += 1
                reply = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
                status = "ok" if result.returncode == 0 else f"exit {result.returncode}"
                print(
                    f"[{args.profile} r{round_no + 1} c{call_no}] {line}\n    -> {status}: {' | '.join(reply[-3:])[:260]}",
                    flush=True,
                )
                time.sleep(0.5)
            lk("stop")
    print(f"done: {turns} spoken turns for profile {args.profile}")


if __name__ == "__main__":
    main()
