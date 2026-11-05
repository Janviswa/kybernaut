import pytest

from scripts.validate_junit_report import (
    REDACTION_SENTINELS,
    ReportValidationError,
    validate_junit_report,
)


def _write_report(path, content):
    path.write_text(content, encoding="utf-8")
    return path


def test_valid_junit_report_passes_redaction_validation(tmp_path):
    report = _write_report(
        tmp_path / "results.xml",
        '<testsuites><testsuite tests="1" failures="0">'
        '<testcase classname="example" name="test_ok"/>'
        "</testsuite></testsuites>",
    )

    validate_junit_report(report, forbidden_values=("ci-only-token",))


@pytest.mark.parametrize(
    "sentinel",
    REDACTION_SENTINELS,
    ids=("secret-sentinel", "token-sentinel", "credential-sentinel"),
)
def test_report_with_each_synthetic_secret_sentinel_is_rejected(
    tmp_path, sentinel
):
    report = _write_report(
        tmp_path / "results.xml",
        "<testsuites><testsuite><testcase classname=\"example\" "
        f'name="test_failure"><failure>{sentinel}</failure></testcase>'
        "</testsuite></testsuites>",
    )

    with pytest.raises(ReportValidationError, match="forbidden sentinel"):
        validate_junit_report(report)


@pytest.mark.parametrize(
    "content",
    [
        "<testsuites><testsuite>",
        "<not-junit><testcase name='test'/></not-junit>",
        "<testsuites/>",
    ],
)
def test_malformed_or_invalid_junit_report_is_rejected(tmp_path, content):
    report = _write_report(tmp_path / "results.xml", content)

    with pytest.raises(ReportValidationError):
        validate_junit_report(report)


def test_report_containing_configured_ci_credential_is_rejected(tmp_path):
    report = _write_report(
        tmp_path / "results.xml",
        '<testsuites><testsuite><testcase name="test">'
        "<system-out>ci-only-secret</system-out>"
        "</testcase></testsuite></testsuites>",
    )

    with pytest.raises(ReportValidationError, match="forbidden sentinel"):
        validate_junit_report(report, forbidden_values=("ci-only-secret",))


def test_report_containing_credential_assignment_is_rejected(tmp_path):
    report = _write_report(
        tmp_path / "results.xml",
        '<testsuites><testsuite><testcase name="test">'
        "<system-out>API_KEY=synthetic-value</system-out>"
        "</testcase></testsuite></testsuites>",
    )

    with pytest.raises(ReportValidationError, match="credential-like"):
        validate_junit_report(report)


def test_missing_report_is_rejected(tmp_path):
    with pytest.raises(ReportValidationError, match="missing"):
        validate_junit_report(tmp_path / "missing.xml")
