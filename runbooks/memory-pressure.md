# Memory Pressure and Possible Memory Leak

## Symptoms
- Sustained growth in resident memory.
- Out-of-memory termination or container restarts.
- Increasing latency under stable workload.

## Evidence to collect
- Resident memory over time.
- Container memory limits and OOM events.
- Restart counts and application logs.
- Request rate and recent deployments.

## Investigation
1. Determine whether memory is growing consistently or fluctuating normally.
2. Check for OOMKilled events and memory-limit pressure.
3. Compare memory growth with traffic.
4. Inspect recent changes and possible retained objects.

## Safe response
- Do not assume a memory leak from one high reading.
- Recommend a controlled investigation and, where justified, a restart plan.
- Require policy approval before restarting or changing resources.
