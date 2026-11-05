import hashlib
import json
from types import SimpleNamespace

import pytest

from app.services import anomaly_runtime
from scripts import evaluate_mad_holdout, evaluate_per_service_mad
from scripts import evaluate_anomaly_benchmark as benchmark


def _modified_baseline(tmp_path):
    approved = anomaly_runtime.BASELINE_PATH.read_bytes()
    modified = approved.replace(
        b"0.05423728813559322",
        b"0.06423728813559322",
        1,
    )
    assert modified != approved
    path = tmp_path / anomaly_runtime.BASELINE_FILENAME
    path.write_bytes(modified)
    return path


def test_approved_baseline_is_accepted_with_runtime_identity():
    rows, digest = benchmark._read_baseline(anomaly_runtime.BASELINE_PATH)

    assert len(rows) == 240
    assert digest == anomaly_runtime.EXPECTED_BASELINE_SHA256


def test_modified_custom_baseline_requires_explicit_exploratory_mode(
    tmp_path,
):
    path = _modified_baseline(tmp_path)

    with pytest.raises(
        benchmark.AnomalyBenchmarkIncomplete,
        match="baseline_digest_mismatch",
    ):
        benchmark._read_baseline(path)

    rows, digest = benchmark._read_baseline(path, exploratory=True)
    assert len(rows) == 240
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()
    assert digest != anomaly_runtime.EXPECTED_BASELINE_SHA256


def test_reproducibility_metadata_identifies_sources_and_label_provenance():
    _, digest = benchmark._read_baseline(anomaly_runtime.BASELINE_PATH)

    metadata = benchmark.build_reproducibility_metadata(
        baseline_path=anomaly_runtime.BASELINE_PATH,
        baseline_sha256=digest,
        exploratory=False,
        evaluator_path=benchmark.__file__,
        configuration={"training_fraction": 0.7},
        random_seeds={"fixture": 123},
        label_provenance={
            "synthetic": True,
            "assumed_normal": True,
            "independently_reviewed_labels_present": False,
        },
    )
    benchmark._validate_reproducibility_metadata(
        metadata, baseline_sha256=digest
    )

    assert metadata["baseline_version"] == "v6"
    assert metadata["baseline_sha256"] == digest
    assert metadata["source_revision"]
    assert metadata["source_tree_state"] in {"clean", "modified", "unknown"}
    assert set(metadata["source_sha256"]) == {
        "evaluator", "runtime", "policy", "detector"
    }
    assert metadata["python_version"]
    assert metadata["random_seeds"] == {"fixture": 123}
    assert (
        metadata["label_provenance"]
        ["independently_reviewed_labels_present"] is False
    )


def test_modified_source_tree_is_recorded_without_paths(monkeypatch):
    def run_git(args, **kwargs):
        if args[1:3] == ["-C", str(benchmark.PROJECT_ROOT)]:
            return SimpleNamespace(
                stdout=" M private-name-is-not-recorded\n"
            )
        return SimpleNamespace(stdout=f"{'a' * 40}\n")

    monkeypatch.setattr(benchmark.subprocess, "run", run_git)

    metadata = benchmark.build_reproducibility_metadata(
        baseline_path=anomaly_runtime.BASELINE_PATH,
        baseline_sha256=anomaly_runtime.EXPECTED_BASELINE_SHA256,
        exploratory=False,
        evaluator_path=benchmark.__file__,
        configuration={},
        random_seeds={},
        label_provenance={
            "independently_reviewed_labels_present": False,
        },
    )

    assert metadata["source_tree_state"] == "modified"
    assert "private-name-is-not-recorded" not in json.dumps(metadata)


def test_exploratory_metadata_is_explicit_and_not_runtime_approved(
    tmp_path,
):
    path = _modified_baseline(tmp_path)
    _, digest = benchmark._read_baseline(path, exploratory=True)
    metadata = benchmark.build_reproducibility_metadata(
        baseline_path=path,
        baseline_sha256=digest,
        exploratory=True,
        evaluator_path=benchmark.__file__,
        configuration={},
        random_seeds={},
        label_provenance={"independently_reviewed_labels_present": False},
    )

    assert metadata["evaluation_mode"] == "exploratory"
    assert metadata["baseline_version"] is None
    assert metadata["baseline_sha256"] == digest
    assert "EXPLORATORY" in benchmark._artifact_freshness(
        {"reproducibility": metadata}
    )


def test_exploratory_artifact_is_validated_and_carries_supplied_digest(
    tmp_path,
):
    path = _modified_baseline(tmp_path)
    artifact = benchmark.build_anomaly_benchmark(
        baseline_path=path,
        exploratory=True,
    )

    assert artifact["reproducibility"]["evaluation_mode"] == "exploratory"
    assert artifact["reproducibility"]["baseline_version"] is None
    assert artifact["reproducibility"]["baseline_sha256"] == (
        hashlib.sha256(path.read_bytes()).hexdigest()
    )
    assert benchmark.validate_anomaly_artifact(artifact) == artifact["metrics"]


@pytest.mark.parametrize(
    "evaluator",
    [evaluate_mad_holdout, evaluate_per_service_mad],
)
def test_standalone_evaluators_reject_custom_default_and_allow_explicit_mode(
    evaluator,
    tmp_path,
    capsys,
):
    path = _modified_baseline(tmp_path)

    with pytest.raises(SystemExit) as error:
        evaluator.main(["--baseline", str(path)])
    assert error.value.code == 2
    assert "baseline_digest_mismatch" in capsys.readouterr().err

    evaluator.main(["--exploratory-baseline", str(path)])
    output = capsys.readouterr().out
    assert '"evaluation_mode": "exploratory"' in output
    assert '"baseline_version": null' in output


def test_new_artifact_validates_metadata_and_rejects_digest_mismatch():
    artifact = benchmark.build_anomaly_benchmark()
    assert artifact["schema_version"] == benchmark.SCHEMA_VERSION
    assert artifact["reproducibility"]["evaluation_mode"] == (
        "approved_baseline_engineering_check"
    )
    assert benchmark.validate_anomaly_artifact(artifact) == artifact["metrics"]

    artifact["reproducibility"]["baseline_sha256"] = "0" * 64
    with pytest.raises(
        benchmark.AnomalyBenchmarkIncomplete,
        match="Reproducibility metadata is invalid",
    ):
        benchmark.validate_anomaly_artifact(artifact)


def test_historical_schema_remains_validatable_without_rewriting_artifact():
    historical = json.loads(
        (
            benchmark.PROJECT_ROOT
            / "benchmark/results/three-way-anomaly-benchmark-v4.json"
        ).read_text(encoding="utf-8")
    )

    assert historical["schema_version"] == 4
    assert "reproducibility" not in historical
    assert benchmark.validate_anomaly_artifact(historical) == (
        historical["metrics"]
    )
    assert "HISTORICAL" in benchmark._artifact_freshness(historical)
