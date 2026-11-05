#!/usr/bin/env python3
"""Compute metrics from recorded results; never injects or fabricates data.

See docs/EVALUATION.md for the results schema and healthy controls.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.benchmark_metrics import (  # noqa: E402
    BenchmarkIncomplete, compute_metrics,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", required=True)
    ap.add_argument(
        "--scenarios",
        help="Scenario matrix JSON (defaults to benchmark/scenarios.json).",
    )
    args = ap.parse_args()
    try:
        data = json.loads(Path(args.results).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"INCOMPLETE: unable to read valid results JSON ({type(exc).__name__}).",
              file=sys.stderr)
        return 2

    if not isinstance(data, dict):
        print("INCOMPLETE: results JSON must contain an object.", file=sys.stderr)
        return 2
    schema_version = None
    if "schema_version" in data:
        schema_version = data["schema_version"]
        if type(schema_version) is not int or schema_version not in {1, 2}:
            print(
                "INCOMPLETE: schema_version must be integer 1 or 2.",
                file=sys.stderr,
            )
            return 2
    if schema_version == 2 and "run" not in data:
        print(
            "INCOMPLETE: schema_version 2 requires a run envelope.",
            file=sys.stderr,
        )
        return 2
    if schema_version != 2 and "run" in data:
        print(
            "INCOMPLETE: a run envelope requires schema_version 2.",
            file=sys.stderr,
        )
        return 2

    try:
        metrics = compute_metrics(
            data.get("cases", []),
            data.get("controls", []),
            scenario_matrix_path=args.scenarios,
            schema_version=schema_version,
            run_envelope=data.get("run"),
        )
    except BenchmarkIncomplete as exc:
        print(f"INCOMPLETE: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
