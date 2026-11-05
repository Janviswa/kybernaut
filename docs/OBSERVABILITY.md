# Observability Operations Handover

This runbook describes the Docker syslog → Grafana Alloy → Loki configuration
for the four Kybernaut application services. It is an operational reference,
not authorization to change or recreate containers.
See [`OBSERVABILITY_RESILIENCE_PLAN.md`](./OBSERVABILITY_RESILIENCE_PLAN.md)
for separately gated, disposable resilience tests; none are authorized by
that plan.

## Architecture and labels

```text
Order / Payment / User / Inventory containers
  → Docker syslog driver (TCP, RFC5424 microsecond timestamps)
  → Alloy receiver at host 127.0.0.1:1516 (full local stack)
    or 127.0.0.1:1515 (isolated observability stack)
  → Loki at http://loki:3100 on the observability network
  → count-only or log queries
```

Each fixed Docker syslog tag is allowlisted and mapped to one bounded Loki
`service` label:

| Docker tag (RFC5424 APP-NAME) | Loki label |
| --- | --- |
| `kybernaut-order` | `service="order"` |
| `kybernaut-payment` | `service="payment"` |
| `kybernaut-user` | `service="user"` |
| `kybernaut-inventory` | `service="inventory"` |

The full local stack's application containers use Docker's `syslog` driver
with:

```text
syslog-address=tcp://127.0.0.1:1516
syslog-format=rfc5424micro
mode=non-blocking
max-buffer-size=4m
```

The isolated observability stack publishes its Alloy listener on port `1515`;
it does not include the application services. Do not point the application
containers at that isolated listener unless their network and Loki routing
are deliberately configured for it.

Non-blocking mode prevents a full or unavailable logging destination from
blocking application writes indefinitely. Its buffer is bounded; messages can
be dropped when it fills. Alloy has no persistent write-ahead log configured.
Delivery is not guaranteed or lossless.

## Isolation and configuration

- Loki has no host-published port. Its HTTP API is available to Alloy at
  `http://loki:3100` on the Compose network shared by the services.
- Alloy's host-published syslog port is bound only to host loopback: `1516`
  for the full local stack and `1515` for the isolated stack.
  The receiver itself is unauthenticated; peers attached to the dedicated
  observability network can reach it.
- The isolated stack uses the dedicated `kybernaut-observability_backend`
  network and `kybernaut-observability_loki-data` volume. The full local stack
  uses its Compose default network and `loki-data` volume.
- Config files are mounted read-only. The logging collectors do not mount the
  Docker socket or host application-log directories; Docker sends syslog
  records to Alloy instead. No host log paths or additional host permissions
  are required.
- Compose health checks probe Loki's `/ready` endpoint and Alloy's `/-/ready`
  endpoint. Alloy waits for Loki to become healthy at startup.
- A Docker health check reports readiness; it does not restart a process that
  remains running but becomes unhealthy. The `unless-stopped` restart policy
  applies when a container exits, not when it is merely unhealthy. Recovery
  from a persistent unhealthy state therefore requires operator diagnosis and
  an explicitly chosen service-level recovery action.

Relevant files:

- Isolated services and ports: [`docker-compose.observability.yml`](../docker-compose.observability.yml)
- Full local-stack Alloy listener: `docker-compose.yml` publishes Alloy on
  host loopback port `1516`; the isolated stack uses port `1515`.
- The app-only `docker-compose.app-services.yml` overlay assumes the
  full-stack Alloy listener is already available; it does not manage or wait
  for Alloy.
- Alloy receiver, allowlist, and Loki writer: [`monitoring/alloy/config.alloy`](../monitoring/alloy/config.alloy)
- Loki storage and retention settings: [`monitoring/loki/loki-config.yml`](../monitoring/loki/loki-config.yml)

The legacy files under `monitoring/promtail/` remain in the repository but
are not referenced by either active Compose configuration.

## Read-only operational checks

Run commands from the repository root. These checks do not display environment
values or application log messages.

### Container state and readiness

```bash
docker inspect --format '{{.Name}} state={{.State.Status}}' \
  kybernaut-observability-loki kybernaut-observability-alloy

# Loki readiness; use the container's current IP, not a published host port.
LOKI_IP="$(docker inspect --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' kybernaut-observability-loki)"
curl --silent --show-error --max-time 5 \
  --output /dev/null --write-out 'HTTP=%{http_code}\n' \
  "http://${LOKI_IP}:3100/ready"

# Alloy's HTTP server listens on 127.0.0.1:12345 inside its container.
docker exec kybernaut-observability-alloy /usr/bin/bash -c \
  'exec 3<>/dev/tcp/127.0.0.1/12345; printf "GET /-/ready HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n" >&3; IFS= read -r response <&3; printf "%s\n" "$response"'
```

Require Loki HTTP 200 with body `ready`, and Alloy HTTP 200. Docker state
`running` alone is not a readiness check.

### Health and recovery behavior

Docker records health-check results independently from process state. Inspect
both state and the latest health-probe output without printing environment
values:

```bash
docker inspect --format \
  '{{.Name}} state={{.State.Status}} health={{if .State.Health}}{{.State.Health.Status}}{{else}}not-configured{{end}} restart_count={{.RestartCount}}' \
  kybernaut-observability-loki kybernaut-observability-alloy
```

`unless-stopped` restarts containers after process exit, but it does not restart
a still-running unhealthy process. Compose's `service_healthy` dependency
conditions gate dependent startup on Loki readiness; they do not provide
continuous dependency monitoring or restart already-running dependents when
Loki later becomes unhealthy. For a persistent unhealthy status, inspect the
service logs and its dependencies first, then select and authorize a targeted
recovery action. Do not treat a healthy process state as proof that log delivery
is lossless.

### Application health endpoints

```bash
for port in 8001 8002 8003 8004; do
  printf 'port=%s ' "$port"
  curl --silent --show-error --max-time 5 \
    --output /dev/null --write-out 'HTTP=%{http_code}\n' \
    "http://127.0.0.1:${port}/health"
done
```

Require HTTP 200 for all four services.

### Count-only Loki queries

These LogQL queries return counts, not log lines. Use a recent interval such
as `[5m]`; a zero can mean no recent messages, not necessarily a broken
pipeline.

```logql
sum(count_over_time({service="order"}[5m]))
sum(count_over_time({service="payment"}[5m]))
sum(count_over_time({service="user"}[5m]))
sum(count_over_time({service="inventory"}[5m]))
```

To inspect only the service-label inventory and counts in that interval:

```logql
sum by (service) (count_over_time({service=~".+"}[5m]))
```

The expected service values are exactly `order`, `payment`, `user`, and
`inventory`. This grouped query does not display application messages.

Loki's HTTP API is not published on the host. To query it from the existing
Alloy container, use its shell TCP connection to Loki and an encoded query.
For example, this count-only request returns the HTTP response and count JSON,
not log entries:

```bash
docker exec kybernaut-observability-alloy /usr/bin/bash -c \
  'exec 3<>/dev/tcp/loki/3100; printf "GET /loki/api/v1/query?query=sum%28count_over_time%28%7Bservice%3D%22order%22%7D%5B5m%5D%29%29 HTTP/1.1\r\nHost: loki\r\nConnection: close\r\n\r\n" >&3; cat <&3'
```

Change only the fixed label in the encoded query when checking another
service. Do not add a log-returning endpoint or print application payloads in
routine acceptance checks.

### Logging driver and option inspection

Inspect only logging fields; do not print `.Config.Env` or use an unrestricted
container inspect dump:

```bash
docker inspect --format '{{.Name}} driver={{.HostConfig.LogConfig.Type}} options={{json .HostConfig.LogConfig.Config}}' \
  kybernaut-order-service kybernaut-payment-service \
  kybernaut-user-service kybernaut-inventory-service
```

Expected driver: `syslog`; expected fixed tags are listed above, with the
configured TCP address, `rfc5424micro`, `mode=non-blocking`, and
`max-buffer-size=4m`.

### Rollback-backup inventory

The following original containers are intentionally stopped, disconnected
from the application network, and retained with their original
`json-file` logging configuration:

- `kybernaut-order-service-rollback-e5a`
- `kybernaut-payment-service-rollback-e5b`
- `kybernaut-user-service-rollback-e5c`
- `kybernaut-inventory-service-rollback-e5d`

Read-only state and logging inspection:

```bash
docker inspect --format '{{.Name}} state={{.State.Status}} image={{.Image}} driver={{.HostConfig.LogConfig.Type}} options={{json .HostConfig.LogConfig.Config}} networks={{json .NetworkSettings.Networks}}' \
  kybernaut-order-service-rollback-e5a \
  kybernaut-payment-service-rollback-e5b \
  kybernaut-user-service-rollback-e5c \
  kybernaut-inventory-service-rollback-e5d
```

## Controlled rollback procedure

Rollback is a separately authorized, service-by-service operation. **Do not
run a rollback based only on this documentation.** Before any rollback,
inspect the current replacement and the corresponding backup, capture their
runtime configuration without exposing environment values, and verify image
IDs, command, workdir, hostname, restart policy, labels, port binding, mounts,
and network aliases. Plan the application name, service alias, and port
handoff before taking action. Preserve the current replacement for
investigation; do not delete either container or guess a recreation command.

## Recorded validation and limitations

Phase E.6 reported the following at `2026-10-22T09:44:50Z`:

- All four application health endpoints returned HTTP 200.
- Recent five-minute count-only Loki queries each returned HTTP 200 with one
  series: Order **65**, Payment **64**, User **55**, Inventory **25**.
- The grouped query returned only the four expected service labels.
- Loki and Alloy were running and ready; Prometheus and Grafana were running.
- All four original rollback backups were retained.
- The configured Loki retention period is **168h (seven days)**. This is
  configuration-verified; actual retention deletion behavior is unverified.

The Payment image ID discrepancy noted during Phase E.6 is preserved here:
the inspected original Payment container and live replacement use
`sha256:cf7c89892f609ff69cb8d7e8d3b5743592f25ef39488884f166f45f69f6e2166`.
That matches the original ID captured in the live runtime inventory but
differs from the hash stated in the earlier Phase E.5B request. No image or
container changes were made during Phase E.6. Treat the verified original
runtime image ID as the migration baseline unless further evidence establishes
otherwise; do not pull or recreate an image to reconcile the discrepancy.

The recorded runtime checks below predate the current Compose-only change and
do not validate the updated Compose configuration. The following remain
unverified for that configuration:

- Logging behavior under sustained load.
- Delivery during Loki or Alloy outages and recovery afterward.
- Lossless delivery under buffer saturation.
- Actual seven-day retention deletion.

Successful canaries and recent count queries establish that messages traversed
the tested path at those times. They do not prove sustained reliability or
lossless delivery.
