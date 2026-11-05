# Pod Not Ready

## Symptoms
- Pod is Running but Ready is False; Service has no endpoints.
- Readiness probe failures in events.

## Evidence to collect
- Readiness probe configuration and failure messages.
- Dependency health the probe relies on.
- Deployment available replicas versus desired.

## Investigation
1. Confirm which probe fails and why: timeout, wrong port, or path.
2. Check that the application finished starting.
3. Compare with healthy replicas of the same Deployment.

## Safe response
- Do not restart solely because readiness is false; find the probe cause.
- Prefer investigating dependencies before changing replicas.
