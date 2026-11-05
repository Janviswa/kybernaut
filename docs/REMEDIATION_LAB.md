# Remediation lab — manual procedure (Kind only)

## 1. Start the API in live mode (this process only)
    kubectl config use-context kind-kybernaut
    # stop any older API on port 8010 first (it is usually the dry_run one)
    ./scripts/dev_live.sh                      # sets KYBERNAUT_REMEDIATION_MODE=live
    cd UI && npm run dev -- --host 127.0.0.1 --port 5173
Check: GET /agent/status -> remediation.mode == "live".

## 2. One-time lab setup
    python scripts/remediation_lab.py preload   # docker pull + kind load busybox:1.36 (no registry needed later)
    python scripts/remediation_lab.py apply     # creates 3 deployments at replicas 0

## 3. Create a failure (pick one)
    python scripts/remediation_lab.py up bad-image    # ImagePullBackOff
    python scripts/remediation_lab.py up crash-loop   # CrashLoopBackOff
    python scripts/remediation_lab.py up disk-evict   # one bounded 60Mi write into a 40Mi emptyDir -> Evicted
Evicted-pod evidence is only counted for 30 minutes (POD_EVENT_LOOKBACK): approve within that window.

## 4. In the console
Run an investigation on the new incident -> Remediation tab shows the option:
bad-image: set_image | crash-loop: restore_command (restart is shown rejected, with reason) | disk-evict: restart_pod.
Approve with a second operator (requester cannot approve own action), then Execute.

## 5. Verify on the cluster
    kubectl --context kind-kybernaut -n kybernaut-lab get deploy,pods
    kubectl --context kind-kybernaut -n kybernaut-lab get deploy sim-bad-image -o jsonpath='{.spec.template.spec.containers[0].image}'
    kubectl --context kind-kybernaut -n kybernaut-lab get deploy sim-crash-loop -o jsonpath='{.spec.template.spec.containers[0].command}'

## 6. Repeat
    python scripts/remediation_lab.py reset bad-image   # (or crash-loop / disk-evict)