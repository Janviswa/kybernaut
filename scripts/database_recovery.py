#!/usr/bin/env python3
"""Disposable-only SQLite recovery utility for Kybernaut."""

from __future__ import annotations

import argparse
import json
import sys

from app.services.database_recovery import (
    RecoveryError,
    create_backup,
    restore_disposable,
    validate_database,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create, validate, or materialize SQLite snapshots beneath the "
            "system temporary directory. Not a production backup system."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)

    backup = commands.add_parser(
        "backup", help="Create an online SQLite snapshot"
    )
    backup.add_argument(
        "--source", required=True, help="Explicit disposable SQLite source"
    )
    backup.add_argument(
        "--destination",
        required=True,
        help="New snapshot path under the temp directory",
    )
    backup.add_argument(
        "--confirm-disposable-source",
        action="store_true",
        help="Confirm that the source contains only disposable test data",
    )
    backup.add_argument(
        "--create-backup",
        action="store_true",
        help="Explicitly authorize creation of the selected backup",
    )

    validate = commands.add_parser(
        "validate", help="Read-only integrity and lifecycle validation"
    )
    validate.add_argument(
        "--database",
        required=True,
        help="Explicit database path under the temp directory",
    )

    restore = commands.add_parser(
        "restore", help="Materialize a snapshot at a fresh disposable path"
    )
    restore.add_argument(
        "--backup",
        required=True,
        help="Explicit backup path under the temp directory",
    )
    restore.add_argument(
        "--destination",
        required=True,
        help="New restore path under the temp directory",
    )
    restore.add_argument(
        "--confirm-disposable-destination",
        action="store_true",
        help="Confirm that the destination is fresh and disposable",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(
                args.source,
                args.destination,
                create_backup=args.create_backup,
                confirm_disposable_source=args.confirm_disposable_source,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if args.command == "validate":
            result = validate_database(args.database)
            print(json.dumps(result, sort_keys=True))
            return 0 if result["valid"] else 2
        result = restore_disposable(
            args.backup,
            args.destination,
            confirm_disposable_destination=args.confirm_disposable_destination,
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except RecoveryError as exc:
        print(f"database-recovery: {exc}", file=sys.stderr)
        return 2
    except OSError:
        print(
            "database-recovery: filesystem operation failed; "
            "destination state requires inspection.",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
