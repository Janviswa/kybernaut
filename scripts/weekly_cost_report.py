#!/usr/bin/env python3
"""Create a local weekly cost report; email requires two explicit opt-ins."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from app.services.cost_optimizer import load_catalog  # noqa: E402
from app.services.cost_telemetry import (  # noqa: E402
    CostTelemetryError,
    collect_kubernetes_cost_evidence,
    load_query_profile,
)
from app.services.kubernetes_cost_model import (  # noqa: E402
    KubernetesPricingError,
    load_kubernetes_pricing,
)
from app.services.weekly_cost_report import (  # noqa: E402
    DEFAULT_RECIPIENT,
    WeeklyCostConfig,
    WeeklyCostReportError,
    WeeklyRunLedger,
    analyze_week,
    default_recipient,
    last_completed_week,
    load_observation_file,
    render_weekly_markdown,
    send_weekly_report,
    synthetic_weekly_observations,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--observations", help="Timestamped observation JSON")
    inputs.add_argument(
        "--synthetic", action="store_true",
        help="Use deterministic local sample data",
    )
    inputs.add_argument(
        "--live-collect", action="store_true",
        help="Read Prometheus range data and allowlisted Kubernetes lab metadata",
    )
    period = parser.add_mutually_exclusive_group()
    period.add_argument(
        "--last-completed-week",
        action="store_true",
        help="Use the previous full Monday-to-Monday week",
    )
    period.add_argument("--period-start", help="Explicit ISO timestamp")
    parser.add_argument("--period-end", help="Explicit ISO timestamp (exclusive)")
    parser.add_argument(
        "--timezone", default="UTC",
        help="IANA timezone for --last-completed-week (default: UTC)",
    )
    parser.add_argument("--sample-interval-minutes", type=int, default=60)
    parser.add_argument(
        "--prometheus-url",
        default=os.getenv("KYBERNAUT_COST_PROMETHEUS_URL"),
        help="Prometheus base URL (or KYBERNAUT_COST_PROMETHEUS_URL)",
    )
    parser.add_argument("--cpu-query", help="PromQL returning CPU cores by pod/container")
    parser.add_argument(
        "--memory-query", help="PromQL returning memory bytes by pod/container"
    )
    parser.add_argument(
        "--cpu-source-timestamp-query",
        help="PromQL returning raw CPU metric timestamps as Unix seconds",
    )
    parser.add_argument(
        "--memory-source-timestamp-query",
        help="PromQL returning raw memory metric timestamps as Unix seconds",
    )
    parser.add_argument(
        "--query-profile",
        help="Operator-reviewed JSON profile bound to the exact PromQL and labels",
    )
    parser.add_argument(
        "--namespace-label", help="Prometheus label identifying Kubernetes namespace"
    )
    parser.add_argument("--pod-label", help="Prometheus label identifying pod name")
    parser.add_argument(
        "--container-label",
        help="Prometheus label identifying container name",
    )
    parser.add_argument(
        "--workload",
        action="append",
        choices=("demo-web", "sim-cpu-stress", "sim-memory-leak",
                 "sim-disk-fill", "sim-pod-crash"),
        help="Allowlisted lab Deployment to analyze (repeatable; default: demo-web)",
    )
    parser.add_argument(
        "--kubernetes-pricing",
        help="Explicit Kubernetes request-pricing JSON (separate from VM pricing)",
    )
    parser.add_argument(
        "--pricing", default=str(ROOT / "config" / "pricing.example.json")
    )
    parser.add_argument("--out", default=str(ROOT / "reports" / "weekly-cost"))
    parser.add_argument(
        "--recipient", default=None,
        help=f"Report recipient (default setting: {DEFAULT_RECIPIENT})",
    )
    parser.add_argument(
        "--send-email", action="store_true",
        help="Request SMTP delivery; also requires KYBERNAUT_COST_REPORT_EMAIL_ENABLED=true",
    )
    args = parser.parse_args()

    try:
        if args.last_completed_week:
            if args.period_end:
                parser.error(
                    "--period-end cannot be combined with --last-completed-week"
                )
            period_start, period_end = last_completed_week(args.timezone)
        else:
            if not args.period_start or not args.period_end:
                parser.error(
                    "provide both --period-start and --period-end, or "
                    "--last-completed-week"
                )
            period_start, period_end = args.period_start, args.period_end

        if args.synthetic:
            catalog, pricing_meta = load_catalog(args.pricing)
            observations = synthetic_weekly_observations(
                period_start,
                period_end,
                sample_interval_minutes=args.sample_interval_minutes,
            )
            provenance = "deterministic synthetic weekly data; no monitoring source"
            kubernetes_resources = None
            collection = None
            kubernetes_pricing = None
        elif args.live_collect:
            required = {
                "--prometheus-url": args.prometheus_url,
                "--cpu-query": args.cpu_query,
                "--memory-query": args.memory_query,
                "--cpu-source-timestamp-query": args.cpu_source_timestamp_query,
                "--memory-source-timestamp-query": args.memory_source_timestamp_query,
                "--query-profile": args.query_profile,
                "--namespace-label": args.namespace_label,
                "--pod-label": args.pod_label,
                "--container-label": args.container_label,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                parser.error(
                    "live collection requires " + ", ".join(missing)
                )
            result = collect_kubernetes_cost_evidence(
                period_start=period_start,
                period_end=period_end,
                sample_interval_minutes=args.sample_interval_minutes,
                prometheus_url=args.prometheus_url,
                cpu_query=args.cpu_query,
                memory_query=args.memory_query,
                cpu_source_timestamp_query=args.cpu_source_timestamp_query,
                memory_source_timestamp_query=args.memory_source_timestamp_query,
                query_profile=load_query_profile(args.query_profile),
                workloads=tuple(args.workload or ["demo-web"]),
                namespace_label=args.namespace_label,
                pod_label=args.pod_label,
                container_label=args.container_label,
            )
            kubernetes_pricing = (
                load_kubernetes_pricing(args.kubernetes_pricing)
                if args.kubernetes_pricing else None
            )
            observations = result["observations"]
            kubernetes_resources = result["resources"]
            collection = result["collection"]
            catalog, pricing_meta = [], {
                "source": (
                    kubernetes_pricing.source
                    if kubernetes_pricing else
                    "unavailable: no Kubernetes request pricing model"
                ),
                "example": False,
            }
            provenance = (
                "live Prometheus range evidence and read-only "
                "kybernaut-lab Kubernetes metadata"
            )
        else:
            kubernetes_pricing = None
            catalog, pricing_meta = load_catalog(args.pricing)
            source = load_observation_file(args.observations)
            observations = source["observations"]
            provenance = source.get("provenance", "timestamped local input")
            args.sample_interval_minutes = source.get(
                "sample_interval_minutes", args.sample_interval_minutes
            )

        report = analyze_week(
            observations,
            period_start=period_start,
            period_end=period_end,
            sample_interval_minutes=args.sample_interval_minutes,
            catalog=catalog,
            provenance=provenance,
            pricing_meta=pricing_meta,
            config=WeeklyCostConfig.from_env(),
            kubernetes_resources=kubernetes_resources,
            kubernetes_pricing=kubernetes_pricing,
        )
        if args.live_collect:
            report["collection"] = collection
    except (
        CostTelemetryError, KubernetesPricingError,
        WeeklyCostReportError, ValueError, OSError,
    ) as exc:
        parser.error(str(exc))
    run_id = uuid.uuid4().hex
    report["run_id"] = run_id
    markdown = render_weekly_markdown(report)
    output = Path(args.out)
    output.mkdir(parents=True, exist_ok=True)
    start_date = report["reporting_period"]["start"][:10]
    end_date = report["reporting_period"]["end_exclusive"][:10]
    stem = f"weekly-{start_date}-to-{end_date}-{run_id[:8]}"
    json_path = output / f"{stem}.json"
    markdown_path = output / f"{stem}.md"
    preview_path = output / f"{stem}.email-preview.txt"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(markdown, encoding="utf-8")

    recipient = (args.recipient or default_recipient()).strip()
    preview_path.write_text(
        f"To: {recipient}\n"
        "Subject: Kybernaut weekly cost optimization report\n\n"
        f"{markdown}",
        encoding="utf-8",
    )

    delivery_requested = (
        args.send_email
        and os.getenv("KYBERNAUT_COST_REPORT_EMAIL_ENABLED", "").lower()
        == "true"
    )
    ledger = WeeklyRunLedger(output / "weekly-runs.sqlite3")
    period_key = (
        f"{report['reporting_period']['start']}/"
        f"{report['reporting_period']['end_exclusive']}"
    )
    period_json = json.dumps(report["reporting_period"], sort_keys=True)
    if args.send_email and not delivery_requested:
        delivery = {
            "status": "disabled_by_configuration",
            "recipient": recipient,
            "sent": False,
            "reason": "set_email_enabled_true_to_opt_in",
        }
    elif delivery_requested and not ledger.claim_delivery(
        period_key, period_json, run_id
    ):
        delivery = {
            "status": "duplicate_suppressed",
            "recipient": recipient,
            "sent": False,
            "reason": "reporting_period_already_sending_or_sent",
        }
    else:
        delivery = send_weekly_report(
            markdown, recipient=recipient, enabled=delivery_requested
        )

    if delivery["status"] == "sent":
        ledger.record(
            period_key, period_json, "delivered", "sent", run_id
        )
    elif delivery["status"] not in {"duplicate_suppressed"}:
        ledger.record(
            period_key,
            period_json,
            "failed" if delivery["status"] == "failed" else "completed",
            delivery["status"],
            run_id,
        )

    result = {
        "run_id": run_id,
        "status": "completed",
        "reporting_period": report["reporting_period"],
        "resources_analyzed": report["summary"]["resources_analyzed"],
        "potential_monthly_savings": report["summary"][
            "potential_monthly_savings"
        ],
        "realized_savings": report["summary"]["realized_savings"],
        "delivery": delivery,
        "artifacts": {
            "json": str(json_path),
            "markdown": str(markdown_path),
            "email_preview": str(preview_path),
            "run_ledger": str(output / "weekly-runs.sqlite3"),
        },
    }
    print(json.dumps(result, indent=2))
    print("\n" + markdown)
    return 1 if delivery["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
