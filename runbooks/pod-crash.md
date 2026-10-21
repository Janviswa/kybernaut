# Pod Crash and CrashLoopBackOff

## Symptoms
- Pod repeatedly restarts.
- Pod is not Ready.
- Kubernetes reports CrashLoopBackOff or OOMKilled.

## Evidence to collect
- Pod status and restart count.
- Current and previous container logs.
- Kubernetes events.
- Readiness and liveness probe results.
- Recent deployment changes.

## Investigation
1. Confirm the affected namespace and workload.
2. Inspect the termination reason and exit code.
3. Review previous container logs and recent events.
4. Check configuration, dependencies, probes, and resource limits.

## Safe response
- Do not delete or restart pods without an approved plan.
- Recommend rollback only when evidence supports a recent deployment regression.
- Require approval for disruptive actions.
