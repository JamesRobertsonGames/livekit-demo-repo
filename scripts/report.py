"""Latency report from the per-turn JSONL files.

    uv run scripts/report.py                      # print tables for every profile found
    uv run scripts/report.py --compare baseline tuned
    uv run scripts/report.py --write-readme       # refresh the tables inside README.md

Every number in the README latency section is produced here. The README holds
two marker pairs, ``<!-- latency:start -->`` / ``<!-- latency:end -->`` and
``<!-- tuning:start -->`` / ``<!-- tuning:end -->``; ``--write-readme`` replaces
what is between them.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# (column title, turn field). List-valued fields contribute every element.
METRICS: list[tuple[str, str]] = [
    ("End-of-turn delay", "end_of_turn_delay"),
    ("LLM time to first token", "llm_ttft"),
    ("TTS time to first byte", "tts_ttfb"),
    ("Tool duration (parallel batch)", "tool_duration"),
    ("Caller done → tools finished", "tools_done_latency"),
    ('"Checking" line playout', "filler_playout"),
    ("Caller done → first agent audio", "first_audio_latency"),
    ("Caller done → start of answer", "answer_latency"),
]
KINDS = [("no_tool", "No-tool turns"), ("tool", "Tool turns")]


def load_turns(turns_dir: Path) -> list[dict[str, Any]]:
    turns: list[dict[str, Any]] = []
    for path in sorted(turns_dir.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                turn = json.loads(line)
                # Typed turns carry no speech timings; only spoken turns are reported.
                if turn.get("audio", True):
                    turns.append(turn)
    return turns


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile, q in [0, 1]."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def values(turns: list[dict[str, Any]], field: str) -> list[float]:
    out: list[float] = []
    for turn in turns:
        v = turn.get(field)
        if isinstance(v, list):
            out.extend(float(x) for x in v)
        elif v is not None:
            out.append(float(v))
    return out


def ms(seconds: float) -> str:
    return f"{round(seconds * 1000):,} ms"


def cell(vals: list[float]) -> str:
    if not vals:
        return "n/a"
    return f"{ms(percentile(vals, 0.5))} / {ms(percentile(vals, 0.95))}"


def by_profile(turns: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for turn in turns:
        grouped[turn.get("profile", "unknown")].append(turn)
    return dict(grouped)


def latency_table(turns: list[dict[str, Any]], profile: str) -> str:
    split = {kind: [t for t in turns if t["kind"] == kind] for kind, _ in KINDS}
    calls = len({t["call_id"] for t in turns})
    head = " | ".join(f"{title} (n={len(split[kind])})" for kind, title in KINDS)
    lines = [
        f"Profile `{profile}`: {len(turns)} turns from {calls} calls. Cells are median / p95.",
        "",
        f"| Metric | {head} |",
        "| --- | --- | --- |",
    ]
    for title, field in METRICS:
        row = " | ".join(cell(values(split[kind], field)) for kind, _ in KINDS)
        lines.append(f"| {title} | {row} |")
    return "\n".join(lines)


def compare_table(before: list[dict[str, Any]], after: list[dict[str, Any]], a: str, b: str) -> str:
    lines = [
        f"Before is profile `{a}` ({len(before)} turns), after is `{b}` ({len(after)} turns). "
        "Cells are median / p95.",
        "",
        f"| Turn type | Metric | Before (`{a}`) | After (`{b}`) | Median change |",
        "| --- | --- | --- | --- | --- |",
    ]
    for kind, kind_title in KINDS:
        for title, field in METRICS:
            x = values([t for t in before if t["kind"] == kind], field)
            y = values([t for t in after if t["kind"] == kind], field)
            if not x and not y:
                continue
            if x and y:
                delta = percentile(y, 0.5) - percentile(x, 0.5)
                change = f"{'+' if delta >= 0 else '−'}{ms(abs(delta))}"
            else:
                change = "n/a"
            lines.append(f"| {kind_title} | {title} | {cell(x)} | {cell(y)} | {change} |")
    return "\n".join(lines)


def replace_block(text: str, name: str, body: str) -> str:
    start, end = f"<!-- {name}:start -->", f"<!-- {name}:end -->"
    if start not in text or end not in text:
        raise SystemExit(f"README.md is missing the {start} / {end} markers")
    head, rest = text.split(start, 1)
    _, tail = rest.split(end, 1)
    return f"{head}{start}\n{body}\n{end}{tail}"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--turns-dir", type=Path, default=ROOT / "data" / "turns")
    parser.add_argument(
        "--profile",
        help="profile for the main table (default: the --compare 'after' profile, else all)",
    )
    parser.add_argument(
        "--compare", nargs=2, metavar=("BEFORE", "AFTER"), help="two profiles for the tuning table"
    )
    parser.add_argument("--write-readme", action="store_true")
    args = parser.parse_args()

    turns = load_turns(args.turns_dir)
    if not turns:
        sys.exit(f"no turns found in {args.turns_dir}; make some calls first")
    profiles = by_profile(turns)

    main_profiles = (
        [args.profile]
        if args.profile
        else ([args.compare[1]] if args.compare else sorted(profiles))
    )
    missing = [p for p in main_profiles + (args.compare or []) if p not in profiles]
    if missing:
        sys.exit(f"no turns recorded for profile(s) {missing}; found {sorted(profiles)}")

    latency = "\n\n".join(latency_table(profiles[p], p) for p in main_profiles)
    print(latency)
    tuning = None
    if args.compare:
        a, b = args.compare
        tuning = compare_table(profiles[a], profiles[b], a, b)
        print("\n" + tuning)

    if args.write_readme:
        readme = ROOT / "README.md"
        text = replace_block(readme.read_text(), "latency", latency)
        if tuning is not None:
            text = replace_block(text, "tuning", tuning)
        readme.write_text(text)
        print(f"\nupdated {readme}")


if __name__ == "__main__":
    main()
