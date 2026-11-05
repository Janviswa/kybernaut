#!/usr/bin/env python3
"""Fail-closed validation for CI JUnit reports before artifact upload."""

from __future__ import annotations

import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REDACTION_SENTINELS = (
    "SYNTHETIC_JUNIT_SECRET_SENTINEL",
    "SYNTHETIC_JUNIT_TOKEN_SENTINEL",
    "SYNTHETIC_JUNIT_CREDENTIAL_SENTINEL",
)
REQUIRED_CI_ENVIRONMENT_VALUES = (
    "KYBERNAUT_REMEDIATION_TOKEN",
    "KYBERNAUT_JWT_SECRET",
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"\b(?:[A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|API_KEY|CREDENTIAL|"
    r"AUTHORIZATION)[A-Z0-9_]*)\s*[:=]\s*"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_BEARER_CREDENTIAL = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
_JWT = re.compile(
    r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_ENVIRONMENT_DUMP_LINE = re.compile(r"(?m)^\s*[A-Z][A-Z0-9_]{2,}=")


class ReportValidationError(ValueError):
    """The report is missing, malformed, or unsafe to publish."""


def validate_junit_report(
    report_path: str | Path,
    *,
    forbidden_values: tuple[str, ...] = (),
) -> None:
    path = Path(report_path)
    if not path.is_file():
        raise ReportValidationError("JUnit report is missing")

    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError, ValueError) as exc:
        raise ReportValidationError("JUnit report is not readable valid XML") from exc

    if root.tag not in {"testsuites", "testsuite"}:
        raise ReportValidationError("JUnit report has an unexpected root element")
    if not root.findall(".//testcase"):
        raise ReportValidationError("JUnit report contains no test cases")

    report_text_parts = []
    for element in root.iter():
        report_text_parts.extend(element.attrib.values())
        if element.text:
            report_text_parts.append(element.text)
        if element.tail:
            report_text_parts.append(element.tail)
    report_text = "\n".join(report_text_parts)

    values = {
        *REDACTION_SENTINELS,
        *(value for value in forbidden_values if value),
    }
    if any(value in report_text for value in values):
        raise ReportValidationError("JUnit report contains a forbidden sentinel value")
    if (
        _CREDENTIAL_ASSIGNMENT.search(report_text)
        or _BEARER_CREDENTIAL.search(report_text)
        or _JWT.search(report_text)
        or _ENVIRONMENT_DUMP_LINE.search(report_text)
    ):
        raise ReportValidationError(
            "JUnit report contains credential-like content or an environment dump"
        )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: validate_junit_report.py REPORT", file=sys.stderr)
        return 2

    missing = [
        name for name in REQUIRED_CI_ENVIRONMENT_VALUES if not os.environ.get(name)
    ]
    if missing:
        print(
            "JUnit redaction validation lacks required CI sentinel values",
            file=sys.stderr,
        )
        return 1

    try:
        validate_junit_report(
            args[0],
            forbidden_values=tuple(
                os.environ[name] for name in REQUIRED_CI_ENVIRONMENT_VALUES
            ),
        )
    except ReportValidationError as exc:
        print(f"JUnit report validation failed: {exc}", file=sys.stderr)
        return 1

    print("JUnit report passed redaction and structure validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
