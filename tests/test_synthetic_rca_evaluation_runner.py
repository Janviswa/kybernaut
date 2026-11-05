import ast
import builtins
import json
from datetime import datetime, timezone
from pathlib import Path
import socket
from unittest.mock import patch

import pytest

from scripts import run_synthetic_rca_evaluation as runner


ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "benchmark/scenarios.json"
NOTES_PATH = ROOT / "benchmark/llm_evaluation/reference_notes_v1.json"


def _prepared():
    return runner.build_report(
        repository_revision="a" * 40,
        started_at=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )


def test_prepares_exact_positive_scenarios_and_excludes_controls():
    report, inputs = _prepared()
    expected = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    ids = [case["scenario_id"] for case in report["cases"]]
    assert ids == [case["scenario_id"] for case in expected["cases"]]
    assert len(ids) == len(set(ids)) == 20
    assert {item["scenario_id"] for item in inputs} == set(ids)
    assert not {control["control_id"] for control in report["healthy_controls"]} & set(ids)
    assert all(control["submitted_to_provider"] is False for control in report["healthy_controls"])


def test_rejects_duplicate_unexpected_and_missing_scenario_ids():
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    ids = [item["scenario_id"] for item in matrix["cases"]]
    with pytest.raises(runner.PreparationError, match="unique"):
        runner.build_report(scenario_ids=[ids[0], ids[0]], repository_revision="a" * 40)
    with pytest.raises(runner.PreparationError, match="unexpected"):
        runner.build_report(scenario_ids=ids[:-1] + ["unexpected"], repository_revision="a" * 40)
    with pytest.raises(runner.PreparationError, match="missing"):
        runner.build_report(scenario_ids=ids[:-1], repository_revision="a" * 40)


def test_adapts_fixture_only_to_explicit_rca_allowlist(monkeypatch):
    called = []

    def fake_fixture(category, scenario_id):
        called.append((category, scenario_id))
        return {
            "namespace": "kybernaut-lab",
            "deployments": [{"name": "demo-web"}],
            "incidents": [],
        }, {"status": "evaluated", "actionable_candidates": ["cpu_cores_used"]}

    monkeypatch.setattr(runner, "_evidence_for", fake_fixture)
    diagnosis = runner._rca_input_from_fixture("cpu_spike", "cpu-spike-1")
    assert called == [("cpu_spike", "cpu-spike-1")]
    assert set(diagnosis) == {
        "service_diagnosis",
        "kubernetes_observations",
        "telemetry_anomaly",
    }
    assert diagnosis["service_diagnosis"]["evidence_status"] == "synthetic_fixture"
    assert diagnosis["kubernetes_observations"] == {
        "namespace": "kybernaut-lab",
        "incidents": [],
    }
    assert diagnosis["telemetry_anomaly"] == {
        "status": "evaluated",
        "actionable_candidates": ["cpu_cores_used"],
    }


def test_rejects_arbitrary_payloads_and_live_evidence():
    with pytest.raises(runner.PreparationError):
        runner._rca_input_from_fixture({"live": "payload"}, "cpu-spike-1")
    report, inputs = _prepared()
    assert "diagnosis" in inputs[0]
    with pytest.raises(runner.PreparationError, match="scenario ID strings"):
        runner.build_report(scenario_ids=[{"service_diagnosis": {"service": "live"}}])


def test_not_attempted_records_have_no_fabricated_model_results():
    report, _ = _prepared()
    assert all(case["call_status"] == "not_attempted" for case in report["cases"])
    assert all(case["structured_output_status"] == "not_attempted" for case in report["cases"])
    for case in report["cases"]:
        assert case["retry_count"] is None
        assert case["latency_ms"] is None
        assert case["token_count"] is None
        assert case["reviewers"] == []
        assert case["adjudicated_scores"] is None
    assert report["tracks"]["llm_rca"]["token_count_total"] is None


def test_validates_report_against_schema_and_semantic_validator():
    report, _ = _prepared()
    assert runner.validate_prepared_report(report) == []
    report["cases"][0]["scenario_id"] = report["cases"][1]["scenario_id"]
    errors = runner.validate_prepared_report(report)
    assert any("semantic:" in error and "unique" in error for error in errors)


def test_missing_schema_validator_fails_without_interpreter_fallback(monkeypatch, capsys):
    original_import = builtins.__import__

    def import_without_jsonschema(name, *args, **kwargs):
        if name == "jsonschema" or name.startswith("jsonschema."):
            raise ImportError("simulated missing jsonschema")
        return original_import(name, *args, **kwargs)

    def unexpected_subprocess(*args, **kwargs):
        pytest.fail("schema validation must not launch another interpreter")

    monkeypatch.setattr(builtins, "__import__", import_without_jsonschema)
    monkeypatch.setattr(runner, "_repository_revision", lambda: "a" * 40)
    monkeypatch.setattr(runner.subprocess, "run", unexpected_subprocess)

    assert runner.main([]) == 1
    error = capsys.readouterr().err
    assert "unavailable in this Python interpreter" in error
    assert "pip install -r requirements.txt" in error


def test_preparation_has_no_provider_or_network_path(monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("network activity is prohibited in offline preparation")

    monkeypatch.setattr(socket, "socket", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    report, _ = _prepared()
    assert report["approval"]["status"] == "not_requested"
    source = ast.parse(Path(runner.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(source)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    called_names = {
        node.func.id
        for node in ast.walk(source)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "generate_rca" not in imported
    assert "generate_rca" not in called_names
    assert "groq" not in imported


def test_rejects_malformed_source_metadata(tmp_path):
    matrix = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    notes = json.loads(NOTES_PATH.read_text(encoding="utf-8"))
    notes["cases"][0]["expected_outcome_metadata"] = "invented"
    bad_notes = tmp_path / "notes.json"
    bad_notes.write_text(json.dumps(notes), encoding="utf-8")
    with pytest.raises(runner.PreparationError, match="outcome"):
        runner.build_report(
            reference_notes_path=bad_notes,
            repository_revision="a" * 40,
        )

    matrix["cases"][0]["scenario_id"] = matrix["cases"][1]["scenario_id"]
    bad_matrix = tmp_path / "matrix.json"
    bad_matrix.write_text(json.dumps(matrix), encoding="utf-8")
    with pytest.raises(runner.PreparationError, match="unique"):
        runner.build_report(
            matrix_path=bad_matrix,
            reference_notes_path=NOTES_PATH,
            repository_revision="a" * 40,
        )


def test_rejects_malformed_fixture_builder_output(monkeypatch):
    monkeypatch.setattr(runner, "_evidence_for", lambda *_: ([], {}))
    with pytest.raises(runner.PreparationError, match="invalid data"):
        runner._rca_input_from_fixture("cpu_spike", "cpu-spike-1")


def test_cli_prepares_without_writing_result_files(tmp_path, capsys):
    output = tmp_path / "output.json"
    assert runner.main([]) == 0
    stdout = capsys.readouterr().out
    result = json.loads(stdout)
    assert result["status"] == "prepared_not_attempted"
    assert result["provider_calls"] == 0
    assert not output.exists()
