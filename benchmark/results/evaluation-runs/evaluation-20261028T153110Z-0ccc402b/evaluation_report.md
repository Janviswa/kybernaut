# Reproducible Kybernaut Evaluation

- Run ID: `evaluation-20261028T153110Z-0ccc402b`
- Started: `2026-10-28T15:31:10.810618+00:00`
- Completed: `2026-10-28T15:31:15.804143+00:00`
- Mode: `offline_synthetic` (live execution verified: no)
- Revision: `b056a57b1bd4a3f1183978caee343fcf97c5aa02`
- Worktree: `dirty`

## Anomaly detection (synthetic/assumed labels)

Dataset: `three_way_synthetic_anomaly_comparison`, 176/184 eligible observations; 8 unavailable. Services: order-service, payment-service, user-service, inventory-service.

| Method | TP | FP | TN | FN | Precision | Recall/TPR | F1 | FPR | n |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| isolation_forest | 0 | 6 | 102 | 68 | 0.0 | 0.0 | 0.0 | 0.055556 | 176 |
| direction_aware_mad | 40 | 4 | 104 | 28 | 0.909091 | 0.588235 | 0.714286 | 0.037037 | 176 |
| hybrid | 40 | 9 | 99 | 28 | 0.816327 | 0.588235 | 0.683761 | 0.083333 | 176 |

FPR is `FP / (FP + TN)` over evaluated observations labeled normal. Synthetic positives and assumed-normal holdout/control points are not independent operational ground truth.

### Per-service results

| Service | Method | TP | FP | TN | FN | Precision | Recall | F1 | FPR | n |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| order-service | isolation_forest | 0 | 0 | 27 | 17 | None | 0.0 | None | 0.0 | 44 |
| order-service | direction_aware_mad | 10 | 1 | 26 | 7 | 0.909091 | 0.588235 | 0.714286 | 0.037037 | 44 |
| order-service | hybrid | 10 | 1 | 26 | 7 | 0.909091 | 0.588235 | 0.714286 | 0.037037 | 44 |
| payment-service | isolation_forest | 0 | 2 | 25 | 17 | 0.0 | 0.0 | 0.0 | 0.074074 | 44 |
| payment-service | direction_aware_mad | 10 | 0 | 27 | 7 | 1.0 | 0.588235 | 0.740741 | 0.0 | 44 |
| payment-service | hybrid | 10 | 2 | 25 | 7 | 0.833333 | 0.588235 | 0.689655 | 0.074074 | 44 |
| user-service | isolation_forest | 0 | 1 | 26 | 17 | 0.0 | 0.0 | 0.0 | 0.037037 | 44 |
| user-service | direction_aware_mad | 10 | 2 | 25 | 7 | 0.833333 | 0.588235 | 0.689655 | 0.074074 | 44 |
| user-service | hybrid | 10 | 3 | 24 | 7 | 0.769231 | 0.588235 | 0.666667 | 0.111111 | 44 |
| inventory-service | isolation_forest | 0 | 3 | 24 | 17 | 0.0 | 0.0 | 0.0 | 0.111111 | 44 |
| inventory-service | direction_aware_mad | 10 | 1 | 26 | 7 | 0.909091 | 0.588235 | 0.714286 | 0.037037 | 44 |
| inventory-service | hybrid | 10 | 3 | 24 | 7 | 0.769231 | 0.588235 | 0.666667 | 0.111111 | 44 |

Per-feature attribution counts are descriptive only; feature-level precision/recall/F1 are not measured because no feature-level labels exist.

## Incident and policy evaluation

- Synthetic classifier: {'correct': 20, 'denominator': 20, 'accuracy': 1.0, 'failed_scenario_ids': []}
- Synthetic diagnosis fixture availability: {'evaluation_scope': 'synthetic fixtures only', 'status_counts': {'available': 18, 'missing_evidence': 1, 'unavailable': 1}, 'available_count': 18, 'denominator': 20, 'availability_rate': 0.9}
- Synthetic policy contract: {'evaluation_scope': 'pure deterministic policy function', 'matched': 16, 'denominator': 16, 'match_rate': 1.0, 'decision_counts': {'allow': 1, 'deny': 1, 'not_applicable': 4, 'require_approval': 14}, 'failed_scenario_ids': [], 'runtime_safety_status': 'not_measured'}
- Synthetic policy decisions: `{'allow': 1, 'deny': 1, 'not_applicable': 4, 'require_approval': 14}`
- Live MTTD, persistence, end-to-end investigation, and authorized remediation verification: **not measured**.
- No faults were injected and no actions were executed.

## Other metrics

| Metric | Status | Reason |
|---|---|---|
| Remediation safety rate | not_measured | This offline benchmark does not execute the remediation API safety case suite. Unit-test coverage must not be interpreted as an empirical safety rate. No live or test remediation operation was executed by this runner. |
| RCA quality | NOT_MEASURED | No LLM provider was called; the RCA reference labels are explicitly unverified. |
| API latency | not_measured | No API server was exercised and no repeatable request workload was run. |

## Reproducibility and limitations

- Baseline SHA-256: `777724eb6aae20da2104fef327dd691ab31a542be06f0c45fc5f66d8cfcac1a1`
- Dataset SHA-256: `713592c113fa70ab029e666549fe7e759e0aaf2595d4d2489d6e18ac6fe6f8f2`
- Configuration: `{'training_fraction': 0.7, 'minimum_training_rows': 20, 'mad_threshold': 5.0, 'isolation_forest_estimators': 200, 'isolation_forest_contamination': 0.05, 'methods': ['isolation_forest', 'direction_aware_mad', 'hybrid']}`
- Random seeds: `{'synthetic_dataset': 2801, 'isolation_forest': 42}`
- This report is from local synthetic fixtures, not production.
- It does not reproduce live fault detection, real incident persistence, or remediation safety outcomes.
- See the JSON report for complete per-service metrics, evidence labels, formulas, and explicit unmeasured reasons.
