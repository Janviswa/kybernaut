---
kybernaut_resolution:
  id: disk-full-evicted-pod-recreate
  incident_types:
    - disk_full
  required_evidence:
    - evicted
  prerequisites:
    min_ready_replicas: 0
    permissions:
      - remediation
  resolution:
    action: restore_command
    alternatives:
      - restart_pod
      - scale_deployment
      - cleanup_evicted_pod
    expected_verification: deployment_available
  documented_risks:
    - The registered simulator recovery stops the disk writer, restores exactly one replica, and removes only the approved evicted Pod UID.
    - Recreating an evicted pod alone does not fix the writer that filled the volume.
    - The condition will recur unless retention, sizeLimit or log rotation is corrected.
  approval_required: true
---

# Disk Full and Ephemeral Storage Exhaustion

## Symptoms
- Pod Evicted with a message about ephemeral storage or DiskPressure.
- Write failures, "no space left on device" in application logs.

## Evidence to collect
- Pod eviction message and node DiskPressure condition.
- Volume sizeLimit and ephemeral-storage limits.
- Which paths are growing: logs, temp files, caches.

## Investigation
1. Identify the writer: application logs, temp data, or an emptyDir.
2. Check whether log rotation is configured.
3. Confirm whether other pods on the node are affected.

## Safe response
- For the registered `sim-disk-fill` lab workload, apply its registered idle command and restore one replica through explicit human approval. If that recovery has already succeeded, a separate approval-gated action may remove only the exact approved evicted Pod UID after confirming one healthy replica. Resolve only after fresh observations confirm recovery.
- For other workloads, do not restart an evicted pod until the writer and retention policy are understood.
- Never delete persistent volumes or files by hand without explicit approval.
- Recommend a human review of retention, limits, and rotation so the condition does not recur.