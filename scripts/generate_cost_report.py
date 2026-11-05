#!/usr/bin/env python3
"""Generate the weekly cost report from utilization + pricing JSON files.

Usage:
  python scripts/generate_cost_report.py \
      --utilization data/utilization.json \
      --pricing config/pricing.json \
      --out reports/cost

utilization.json: {"resources": [{"name", "instance_type",
                    "cpu_utilization": [..0-1..], "memory_utilization": [..]}]}
For timestamped weekly reports, synthetic fixtures, and optional preview-only
email delivery, use scripts/weekly_cost_report.py. Schedule that manual command
with cron if desired; this script does not create a scheduler.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.cost_optimizer import (  # noqa: E402
    build_report, load_catalog, render_markdown,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--utilization", required=True)
    ap.add_argument("--pricing", required=True)
    ap.add_argument("--out", default=str(ROOT / "reports" / "cost"))
    args = ap.parse_args()

    catalog, meta = load_catalog(args.pricing)
    resources = json.loads(
        Path(args.utilization).read_text(encoding="utf-8"))["resources"]
    report = build_report(resources, catalog, meta)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "latest.json").write_text(json.dumps(report, indent=2))
    (out / "latest.md").write_text(render_markdown(report))
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
