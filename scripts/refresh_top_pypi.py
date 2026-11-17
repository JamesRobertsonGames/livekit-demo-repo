"""Refresh the local list of top PyPI projects used for fuzzy name matching.

Source: https://hugovk.github.io/top-pypi-packages/ (monthly dump of the most
downloaded PyPI projects). Run:  uv run scripts/refresh_top_pypi.py [N]
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

URL = "https://hugovk.github.io/top-pypi-packages/top-pypi-packages.min.json"
OUT = Path(__file__).resolve().parent.parent / "src" / "shipcheck" / "data" / "top_pypi.txt"


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    req = urllib.request.Request(URL, headers={"User-Agent": "shipcheck-refresh/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    rows = data["rows"][:limit]
    header = f"# top {len(rows)} PyPI projects by downloads, source {URL}, snapshot {data['last_update']}\n"
    OUT.write_text(header + "\n".join(r["project"] for r in rows) + "\n")
    print(f"wrote {len(rows)} names to {OUT} (snapshot {data['last_update']})")


if __name__ == "__main__":
    main()
