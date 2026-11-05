---
kybernaut_resolution:
  id: crashloop-pod-restart
  incident_types:
    - pod_crash
  required_evidence:
    - crashloopbackoff
    - restarted after termination reason error
  prerequisites:
    # Applies to restart_pod (removes a possibly serving pod). Spec-correcting
    # actions (restore_command) remove no capacity and are evaluated separately.
    min_ready_replicas: 2
    permissions:
      - remediation
  resolution:
    action: restart_pod
    alternatives:
      - rollback_deployment
      - scale_deployment
      - restore_command
    expected_verification: deployment_available
  documented_risks:
    - A restart does not fix a container whose command or configuration is broken; restore_command applies the registered known-good command instead.
    - Restarting a pod may cause a brief interruption and may not resolve a configuration or dependency fault.
  approval_required: true
---

# CrashLoopBackOff

## Symptoms
- Pod status CrashLoopBackOff with growing back-off delay.
- Event "Back-off restarting failed container".

## Evidence to collect
- Previous container logs and exit code.
- Events for the pod and its Deployment.
- Recent image or configuration changes; probe settings.

## Investigation
1. Read the previous container's logs for the startup error.
2. Check missing configuration, secrets, or unreachable dependencies.
3. Distinguish application crash from probe-induced restarts.

## Safe response
- Do not repeatedly delete a crash-looping pod; it will crash again.
- Recommend rollback only if evidence ties the crash to a recent rollout.
- Require approval for any disruptive action.