# Database Timeout

## Symptoms
- Database connection or query timeouts.
- Increased application latency.
- HTTP 5xx errors associated with database operations.

## Evidence to collect
- Application error logs and timeout messages.
- Database availability and connection saturation.
- Query latency and connection-pool utilization.
- Network errors and recent configuration changes.

## Investigation
1. Confirm that errors indicate a database dependency problem.
2. Distinguish connection failures from slow queries.
3. Check connection-pool limits and database health.
4. Review recent deployments and network changes.

## Safe response
- Do not restart the database based only on application timeouts.
- Recommend targeted diagnostics and escalation when database health is uncertain.
- Require approval for disruptive changes.
