#!/usr/bin/env python3
"""Kind-lab failure simulator CLI (dry-run by default).

  python scripts/failure_simulator.py list
  python scripts/failure_simulator.py inject cpu-spike            # dry run
  python scripts/failure_simulator.py inject cpu-spike --execute  # runs
  python scripts/failure_simulator.py cleanup cpu-spike --execute
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.failure_scenarios import (  # noqa: E402
    SCENARIOS, SimulatorError, run_scenario,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("command", choices=["list", "inject", "cleanup"])
    ap.add_argument("scenario", nargs="?")
    ap.add_argument("--execute", action="store_true",
                    help="actually run kubectl (default is a dry run)")
    args = ap.parse_args()

    if args.command == "list":
        for s in SCENARIOS.values():
            print(json.dumps({
                "id": s.scenario_id, "type": s.incident_type,
                "description": s.description,
                "prerequisites": s.prerequisites,
                "expected_telemetry": s.expected_telemetry,
                "expected_detection": s.expected_detection}, indent=2))
        return 0
    if not args.scenario:
        ap.error("scenario required")
    try:
        print(json.dumps(run_scenario(args.scenario, args.command,
                                      dry_run=not args.execute), indent=2))
    except SimulatorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
