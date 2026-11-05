from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts import diagnose_isolation_forest as diagnostics


SAVED_REPORT = (
    Path(__file__).resolve().parents[1]
    / "benchmark"
    / "results"
    / "isolation-forest-diagnostic-v1.json"
)


@pytest.fixture(scope="module")
def report():
    return diagnostics.build_isolation_forest_diagnostic()


@pytest.fixture
def saved_report():
    return json.loads(SAVED_REPORT.read_text(encoding="utf-8"))


def _resign(report):
    report["integrity"]["payload_sha256"] = (
        diagnostics._integrity_digest(report)
    )


def test_diagnostics_use_shared_frozen_samples_and_reproduce_control(report):
    assert report["source_artifact"]["schema_version"] == 4
    assert report["inputs"]["eligible_observation_count"] == 176
    assert report["inputs"]["unavailable_observation_count"] == 8
    assert report["inputs"]["same_saved_evaluation_samples_for_all_variants"]
    assert report["control_reproduction"]["all_flags_match"] is True
    assert report["control_reproduction"][
        "scores_match_within_1e-12"
    ] is True
    diagnostics.validate_diagnostic_report(report)

    sample_ids = [
        prediction["sample_id"]
        for prediction in report["method_reports"]["unscaled_control"][
            "predictions"
        ]
    ]
    assert len(sample_ids) == len(set(sample_ids)) == 176
    assert all(
        [
            prediction["sample_id"]
            for prediction in method["predictions"]
        ] == sample_ids
        for method in report["method_reports"].values()
    )


def test_diagnostic_variants_change_one_documented_factor(report):
    methods = report["method_reports"]
    control_config = methods["unscaled_control"]["configuration"]
    standardized_config = methods["standardized_diagnostic"]["configuration"]
    assert standardized_config["contamination"] == control_config["contamination"]
    assert standardized_config["random_state"] == control_config["random_state"]
    assert standardized_config["n_estimators"] == control_config["n_estimators"]
    assert standardized_config["variable_changed"] == "feature scaling only"

    for name, contamination in (
        ("unscaled_contamination_0.01", 0.01),
        ("unscaled_contamination_0.10", 0.10),
    ):
        config = methods[name]["configuration"]
        assert config["contamination"] == contamination
        assert config["preprocessing"] == "none"
        assert config["random_state"] == control_config["random_state"]
        assert config["n_estimators"] == control_config["n_estimators"]
        assert config["variable_changed"] == "contamination only"


def test_diagnostic_artifact_is_deterministic_and_detects_tampering(report):
    repeated = diagnostics.build_isolation_forest_diagnostic()
    assert repeated == report

    altered = deepcopy(report)
    altered["method_reports"]["unscaled_control"]["predictions"][0][
        "decision_function"
    ] += 0.01
    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="integrity digest",
    ):
        diagnostics.validate_diagnostic_report(altered)


def test_diagnostic_report_rejects_inconsistent_method_sample_sets(report):
    altered = deepcopy(report)
    altered["method_reports"]["standardized_diagnostic"]["predictions"].pop()
    altered["integrity"]["payload_sha256"] = (
        diagnostics._integrity_digest(altered)
    )
    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="prediction count",
    ):
        diagnostics.validate_diagnostic_report(altered)


def test_diagnostic_output_path_never_overwrites(tmp_path, monkeypatch, capsys):
    output = tmp_path / "existing.json"
    output.write_text("preserve", encoding="utf-8")
    monkeypatch.setattr(
        "sys.argv",
        [
            "diagnose_isolation_forest.py",
            "--output",
            str(output),
        ],
    )

    assert diagnostics.main() == 2
    assert output.read_text(encoding="utf-8") == "preserve"
    assert "File exists" in capsys.readouterr().err


def test_saved_diagnostic_report_passes_control_reproduction_validation(
    saved_report,
):
    assert saved_report["schema_version"] == 1
    diagnostics.validate_diagnostic_report(saved_report)


def test_consistent_prediction_truncation_is_rejected(saved_report):
    altered = saved_report
    for method in altered["method_reports"].values():
        method["predictions"].pop()
    _resign(altered)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="prediction count",
    ):
        diagnostics.validate_diagnostic_report(altered)


def test_single_method_prediction_truncation_is_rejected(saved_report):
    saved_report["method_reports"]["standardized_diagnostic"][
        "predictions"
    ].pop()
    _resign(saved_report)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="prediction count",
    ):
        diagnostics.validate_diagnostic_report(saved_report)


def test_declared_count_must_match_prediction_rows(saved_report):
    saved_report["inputs"]["eligible_observation_count"] -= 1
    _resign(saved_report)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="prediction count",
    ):
        diagnostics.validate_diagnostic_report(saved_report)


@pytest.mark.parametrize(
    "value",
    [None, "176", 0, -1, True, 176.0],
)
def test_invalid_declared_eligible_count_is_rejected(saved_report, value):
    saved_report["inputs"]["eligible_observation_count"] = value
    _resign(saved_report)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="eligible observation count",
    ):
        diagnostics.validate_diagnostic_report(saved_report)


def test_missing_declared_eligible_count_is_rejected(saved_report):
    del saved_report["inputs"]["eligible_observation_count"]
    _resign(saved_report)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="eligible observation count",
    ):
        diagnostics.validate_diagnostic_report(saved_report)


@pytest.mark.parametrize("identity_change", ["duplicate", "missing"])
def test_invalid_prediction_sample_identity_is_rejected(
    saved_report, identity_change,
):
    predictions = saved_report["method_reports"]["unscaled_control"][
        "predictions"
    ]
    if identity_change == "duplicate":
        predictions[1]["sample_id"] = predictions[0]["sample_id"]
        expected_message = "duplicate sample IDs"
    else:
        del predictions[0]["sample_id"]
        expected_message = "prediction identity"
    _resign(saved_report)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match=expected_message,
    ):
        diagnostics.validate_diagnostic_report(saved_report)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("all_flags_match", False),
        ("saved_v4_flag_matches", 175),
        ("eligible_observation_count", 175),
    ],
)
def test_control_flag_reproduction_requires_all_flags_to_match(
    saved_report, field, value,
):
    saved_report["control_reproduction"][field] = value
    _resign(saved_report)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="Control reproduction summary",
    ):
        diagnostics.validate_diagnostic_report(saved_report)


@pytest.mark.parametrize(
    ("delta", "scores_match"),
    [
        (2e-12, True),
        (0.5e-12, False),
    ],
)
def test_score_match_boolean_must_agree_with_tolerance(
    saved_report, delta, scores_match,
):
    summary = saved_report["control_reproduction"]
    summary["maximum_absolute_decision_function_delta"] = delta
    summary["scores_match_within_1e-12"] = scores_match
    _resign(saved_report)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="Control reproduction summary",
    ):
        diagnostics.validate_diagnostic_report(saved_report)


@pytest.mark.parametrize(
    "delta",
    [None, True, "0", float("nan"), float("inf"), -1e-12],
)
def test_invalid_score_delta_is_rejected(saved_report, delta):
    saved_report["control_reproduction"][
        "maximum_absolute_decision_function_delta"
    ] = delta
    try:
        _resign(saved_report)
    except ValueError:
        pass

    with pytest.raises(diagnostics.AnomalyBenchmarkIncomplete):
        diagnostics.validate_diagnostic_report(saved_report)


@pytest.mark.parametrize(
    "field",
    [
        "maximum_absolute_decision_function_delta",
        "scores_match_within_1e-12",
    ],
)
def test_missing_score_summary_field_is_rejected(saved_report, field):
    del saved_report["control_reproduction"][field]
    _resign(saved_report)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="Control reproduction summary",
    ):
        diagnostics.validate_diagnostic_report(saved_report)


@pytest.mark.parametrize(
    ("delta", "scores_match"),
    [
        (2e-12, False),
        (0.5e-12, True),
    ],
)
def test_score_delta_boundary_is_accepted_only_when_matching(
    saved_report, delta, scores_match,
):
    summary = saved_report["control_reproduction"]
    summary["maximum_absolute_decision_function_delta"] = delta
    summary["scores_match_within_1e-12"] = scores_match
    _resign(saved_report)

    if scores_match:
        diagnostics.validate_diagnostic_report(saved_report)
    else:
        with pytest.raises(
            diagnostics.AnomalyBenchmarkIncomplete,
            match="Control reproduction summary",
        ):
            diagnostics.validate_diagnostic_report(saved_report)


def test_control_summary_booleans_and_counts_are_strict(saved_report):
    summary = saved_report["control_reproduction"]
    summary["all_flags_match"] = 1
    summary["saved_v4_flag_matches"] = True
    _resign(saved_report)

    with pytest.raises(
        diagnostics.AnomalyBenchmarkIncomplete,
        match="Control reproduction summary",
    ):
        diagnostics.validate_diagnostic_report(saved_report)
