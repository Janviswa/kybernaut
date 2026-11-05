
import asyncio
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx

from app.services.prometheus_features import collect_service_features


OUTPUT = PROJECT_ROOT / "data" / "telemetry_baseline_v5.csv"
INTERVAL_SECONDS = 30
SAMPLE_COUNT = 60

FIELDS = [
    "timestamp",
    "service",
    "request_rate",
    "p95_latency_seconds",
    "memory_bytes",
    "cpu_cores_used",
]


async def main():
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    # Never append to or overwrite a previous baseline.
    if OUTPUT.exists():
        raise FileExistsError(
            f"{OUTPUT} already exists. Choose a new filename before collecting."
        )

    with OUTPUT.open("x", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()

        async with httpx.AsyncClient(timeout=10.0) as client:
            for sample in range(SAMPLE_COUNT):
                timestamp = datetime.now(timezone.utc).isoformat()

                try:
                    rows = await collect_service_features(client)

                    for service, features in rows.items():
                        writer.writerow({
                            "timestamp": timestamp,
                            "service": service,
                            **features,
                        })

                    handle.flush()
                    print(
                        f"[{sample + 1}/{SAMPLE_COUNT}] "
                        f"{timestamp}: collected {len(rows)}/4 services"
                    )
                except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                    print(
                        f"[{sample + 1}/{SAMPLE_COUNT}] "
                        f"Collection failed: {type(exc).__name__}"
                    )

                if sample < SAMPLE_COUNT - 1:
                    await asyncio.sleep(INTERVAL_SECONDS)

    print(f"\nBaseline data written to {OUTPUT}")


if __name__ == "__main__":
    asyncio.run(main())
