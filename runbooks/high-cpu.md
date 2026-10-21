# High CPU Utilization

## Symptoms
- Sustained high CPU usage.
- Increased request latency or timeouts.
- CPU throttling or overloaded application workers.

## Evidence to collect
- CPU utilization over time.
- Request rate and p95 latency.
- Container CPU limits and throttling.
- Recent deployments and application logs.

## Investigation
1. Confirm that CPU usage is elevated over a meaningful time window.
2. Compare CPU usage with request rate.
3. Check for CPU throttling and recent changes.
4. Identify expensive requests or background tasks.

## Safe response
- Do not scale or restart automatically.
- Recommend scaling only after checking capacity and workload behavior.
- Require policy approval before infrastructure changes.
