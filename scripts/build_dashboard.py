"""Generate observability/grafana/dashboards/shipcheck.json.

The dashboard is generated so the panels stay consistent with the metric names
in src/shipcheck/turn_metrics.py. Run: uv run scripts/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent.parent / "observability/grafana/dashboards/shipcheck.json"
DS = {"type": "prometheus", "uid": "prometheus"}
WINDOW = "$__rate_interval"
FILTER = 'profile=~"$profile"'


def quantile(metric: str, q: float, by: str, extra: str = FILTER) -> str:
    selector = f"{{{extra}}}" if extra else ""
    return f"histogram_quantile({q}, sum by (le, {by}) (rate({metric}_bucket{selector}[{WINDOW}])))"


def timeseries(
    title: str,
    targets: list[tuple[str, str]],
    x: int,
    y: int,
    w: int = 12,
    h: int = 8,
    unit: str = "s",
    description: str = "",
) -> dict[str, Any]:
    return {
        "type": "timeseries",
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "fieldConfig": {
            "defaults": {"unit": unit, "custom": {"spanNulls": True, "showPoints": "auto"}},
            "overrides": [],
        },
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "bottom",
                "calcs": ["lastNotNull", "max"],
            },
            "tooltip": {"mode": "multi"},
        },
        "targets": [
            {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": chr(65 + i)}
            for i, (expr, legend) in enumerate(targets)
        ],
    }


def stat(title: str, expr: str, x: int, y: int, unit: str = "s", w: int = 4) -> dict[str, Any]:
    return {
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": 4},
        "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "value",
            "graphMode": "area",
        },
        "targets": [{"datasource": DS, "expr": expr, "refId": "A"}],
    }


def p50_p95(metric: str, by: str = "kind", extra: str = FILTER) -> list[tuple[str, str]]:
    return [
        (quantile(metric, 0.5, by, extra), f"p50 {{{{{by}}}}}"),
        (quantile(metric, 0.95, by, extra), f"p95 {{{{{by}}}}}"),
    ]


def range_quantile(metric: str, q: float, kind: str) -> str:
    return f'histogram_quantile({q}, sum by (le) (increase({metric}_bucket{{{FILTER}, kind="{kind}"}}[$__range])))'


def main() -> None:
    panels = [
        stat(
            "Answer p50, tool turns", range_quantile("shipcheck_answer_seconds", 0.5, "tool"), 0, 0
        ),
        stat(
            "Answer p95, tool turns", range_quantile("shipcheck_answer_seconds", 0.95, "tool"), 4, 0
        ),
        stat(
            "First audio p50, no-tool turns",
            range_quantile("shipcheck_first_audio_seconds", 0.5, "no_tool"),
            8,
            0,
        ),
        stat(
            "First audio p95, no-tool turns",
            range_quantile("shipcheck_first_audio_seconds", 0.95, "no_tool"),
            12,
            0,
        ),
        stat(
            "Turns",
            f"sum(increase(shipcheck_turns_total{{{FILTER}}}[$__range]))",
            16,
            0,
            unit="short",
        ),
        stat(
            "Upstream failures",
            "sum(increase(shipcheck_upstream_failures_total[$__range])) or vector(0)",
            20,
            0,
            unit="short",
        ),
        timeseries(
            "Caller done → first agent audio",
            p50_p95("shipcheck_first_audio_seconds"),
            0,
            4,
            description="End of caller speech to the first agent audio. On tool turns this is the spoken 'checking' line.",
        ),
        timeseries(
            "Caller done → start of answer",
            p50_p95("shipcheck_answer_seconds"),
            12,
            4,
            description="End of caller speech to the start of the final answer. On tool turns this includes the tool calls and a second LLM response.",
        ),
        timeseries("End-of-turn delay", p50_p95("shipcheck_end_of_turn_delay_seconds"), 0, 12, w=8),
        timeseries("LLM time to first token", p50_p95("shipcheck_llm_ttft_seconds"), 8, 12, w=8),
        timeseries("TTS time to first byte", p50_p95("shipcheck_tts_ttfb_seconds"), 16, 12, w=8),
        timeseries(
            "Tool duration by tool",
            p50_p95("shipcheck_tool_duration_seconds", by="tool", extra=""),
            0,
            20,
            description="Each upstream lookup: PyPI, OSV, Anaconda.",
        ),
        timeseries(
            "Tool batch wall time",
            [
                (
                    f"histogram_quantile(0.5, sum by (le) (rate(shipcheck_tool_batch_seconds_bucket{{{FILTER}}}[{WINDOW}])))",
                    "p50",
                ),
                (
                    f"histogram_quantile(0.95, sum by (le) (rate(shipcheck_tool_batch_seconds_bucket{{{FILTER}}}[{WINDOW}])))",
                    "p95",
                ),
            ],
            12,
            20,
            w=6,
            description="First tool start to last tool end within a turn. The three lookups run in parallel.",
        ),
        timeseries(
            "Upstream failures",
            [
                (
                    f"sum by (source) (increase(shipcheck_upstream_failures_total[{WINDOW}]))",
                    "{{source}}",
                )
            ],
            18,
            20,
            w=6,
            unit="short",
        ),
        timeseries(
            "Turns per minute",
            [
                (
                    f"sum by (kind) (rate(shipcheck_turns_total{{{FILTER}}}[{WINDOW}])) * 60",
                    "{{kind}}",
                )
            ],
            0,
            28,
            w=8,
            unit="short",
        ),
        timeseries(
            "Active calls",
            [("sum(lk_agents_active_job_count)", "active jobs")],
            8,
            28,
            w=8,
            unit="short",
        ),
        timeseries(
            "Tool outcomes",
            [
                (
                    f"sum by (tool, status) (increase(shipcheck_tool_duration_seconds_count[{WINDOW}]))",
                    "{{tool}} {{status}}",
                )
            ],
            16,
            28,
            w=8,
            unit="short",
        ),
    ]
    for i, panel in enumerate(panels, start=1):
        panel["id"] = i
    dashboard = {
        "uid": "shipcheck",
        "title": "Shipcheck voice agent",
        "tags": ["shipcheck", "livekit"],
        "timezone": "browser",
        "schemaVersion": 41,
        "version": 1,
        "refresh": "10s",
        "time": {"from": "now-1h", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "profile",
                    "label": "Tuning profile",
                    "type": "query",
                    "datasource": DS,
                    "query": {
                        "query": "label_values(shipcheck_turns_total, profile)",
                        "refId": "profile",
                    },
                    "refresh": 2,
                    "includeAll": True,
                    "multi": True,
                    "allValue": ".*",
                    "current": {"text": "All", "value": "$__all"},
                }
            ]
        },
        "panels": panels,
    }
    OUT.write_text(json.dumps(dashboard, indent=2) + "\n")
    print(f"wrote {OUT} ({len(panels)} panels)")


if __name__ == "__main__":
    main()
