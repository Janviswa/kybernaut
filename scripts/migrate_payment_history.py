"""Copy the currently running payment service's volatile records to PostgreSQL."""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import sys
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from dotenv import load_dotenv

from target_apps.payment_service import payment_store


def _validate_source_url(source_url: str) -> None:
    parsed = urlsplit(source_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/payments"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Source must be a plain local HTTP /payments endpoint"
        )
    if parsed.hostname.lower() == "localhost":
        return
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError("Source host must be loopback") from exc
    if not address.is_loopback:
        raise ValueError("Source host must be loopback")


def _validate_records(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("Source returned an invalid payment list")
    payments = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Source returned an invalid payment record")
        payment_id = item.get("payment_id")
        order_id = item.get("order_id")
        amount = item.get("amount")
        status = item.get("status")
        if (
            not isinstance(payment_id, str)
            or not payment_id
            or not isinstance(order_id, str)
            or not order_id
            or isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount <= 0
            or not isinstance(status, str)
            or not status
        ):
            raise ValueError("Source returned an invalid payment record")
        payments.append({
            "payment_id": payment_id,
            "order_id": order_id,
            "amount": float(amount),
            "status": status,
        })
    return payments


def migrate(source_url: str) -> int:
    _validate_source_url(source_url)
    opener = build_opener(ProxyHandler({}))
    request = Request(source_url, headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=5) as response:
            records = _validate_records(json.load(response))
    except URLError as exc:
        raise RuntimeError(
            "Could not read payment records from the local service"
        ) from exc
    payment_store.initialize()
    return payment_store.import_payments(records)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-url",
        default="http://127.0.0.1:8002/payments",
        help="loopback URL of the old in-memory payment service",
    )
    args = parser.parse_args(argv)
    try:
        imported = migrate(args.source_url)
    except (ValueError, RuntimeError, payment_store.PaymentStoreError) as exc:
        print(f"Payment history migration failed: {exc}", file=sys.stderr)
        return 1
    print(f"Imported {imported} previously unpersisted payment records.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
