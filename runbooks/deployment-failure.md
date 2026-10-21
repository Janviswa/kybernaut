# Deployment Failure

## Symptoms
- New replicas fail readiness checks.
- Deployment rollout stalls.
- Application errors increase after a release.

## Evidence to collect
- Deployment rollout status.
- Replica availability and pod events.
- Image, configuration, and probe changes.
- Error rates and latency before and after release.

## Investigation
1. Identify the affected deployment and release.
2. Check rollout status and pod readiness.
3. Compare current and previous versions.
4. Determine whether the failure correlates with the latest change.

## Safe response
- Do not roll back automatically.
- Recommend rollback only when evidence supports a release-related regression.
- Require approval before changing the deployment.
