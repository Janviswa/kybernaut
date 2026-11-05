---
kybernaut_resolution:
  id: deployment-failure-rollback
  incident_types:
    - deployment_failure
  required_evidence:
    - imagepullbackoff
    - errimagepull
    - unavailable replicas
  prerequisites:
    min_ready_replicas: 0
    permissions:
      - remediation
  resolution:
    action: rollback_deployment
    alternatives:
      - restart_pod
      - set_image
    expected_verification: deployment_available
  documented_risks:
    - set_image applies only the registered known-good image to a designated lab deployment; it never accepts an image from the request.
    - Rolling back reverts the latest release, including any intended changes in it.
    - A rollback does not fix a bad image reference that is also present in the previous revision.
  approval_required: true
---

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
- A rollback to the previous revision is proposed only when the evidence shows a failed rollout (image pull failure or unavailable replicas).
- Require explicit human approval before changing the deployment.