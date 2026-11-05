---
kybernaut_resolution:
  id: oom-killed-pod-restart
  incident_types:
    - memory_leak
  required_evidence:
    - oomkilled
  prerequisites:
    min_ready_replicas: 2
    permissions:
      - remediation
  resolution:
    action: restart_pod
    alternatives:
      - scale_deployment
      - rollback_deployment
      - restore_command
    expected_verification: deployment_available
  documented_risks:
    - A restart can interrupt in-flight requests and does not fix the underlying memory cause.
    - Restoring a command is limited to the registered simulator's known-good idle command.
  approval_required: true
---

# OOMKilled Container Termination

## Symptoms
- Container last state shows terminated with reason OOMKilled, exit code 137.
- Restart count climbs; pod may enter CrashLoopBackOff.

## Evidence to collect
- Last terminated state and exit code of the container.
- Configured memory limit versus observed working set.
- Whether growth is steady (leak) or a one-time spike (undersized limit).

## Investigation
1. Confirm OOMKilled from the container's last state, not from one metric.
2. Compare memory growth with traffic and uptime.
3. Check for a recent release that changed memory behavior.

## Safe response
- A single OOM does not prove a leak; the limit may simply be too low.
- Propose a pod restart only with approval and at least two ready replicas.
- Raising the limit is a configuration change that needs human approval.
- For the registered `sim-memory-leak` lab simulator only, an approval-gated
  command restore can stop the memory-writing loop using its registered idle
  command; no request-provided command is accepted.