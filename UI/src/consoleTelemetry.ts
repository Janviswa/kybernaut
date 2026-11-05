import { SERVICES } from './data';

type Telemetry = {
  services: Record<string, unknown>;
  unavailable_services: unknown[];
  [key: string]: unknown;
};

type RecordValue = Record<string, unknown>;

const P95_DISPLAY_DECIMALS = 3;
const TELEMETRY_MAX_AGE_MS = 30_000;

const record = (value: unknown): RecordValue | null =>
  value && typeof value === 'object' && !Array.isArray(value)
    ? value as RecordValue
    : null;

export function selectTelemetryServices(
  telemetry: Telemetry,
  service?: string,
): Telemetry {
  if (!service) return telemetry;

  const row = Object.prototype.hasOwnProperty.call(telemetry.services, service)
    ? telemetry.services[service]
    : null;
  const available = row !== null && row !== undefined &&
    !telemetry.unavailable_services.includes(service);

  return {
    ...telemetry,
    services: { [service]: available ? row : null },
    unavailable_services: available ? [] : [service],
  };
}

export function summarizeHighestP95(
  telemetry: Telemetry,
  now = Date.now(),
): string {
  const unavailable = new Set(telemetry.unavailable_services);
  const available: Array<{ service: string; value: number }> = [];
  const missing: string[] = [];

  for (const service of SERVICES) {
    const row = record(telemetry.services[service]);
    const samples = record(row?.sample_timestamps);
    const value = row?.p95_latency_seconds;
    const timestamp = samples?.p95_latency_seconds;
    const validValue = typeof value === 'number' &&
      Number.isFinite(value) && value >= 0;
    const sampleTime = typeof timestamp === 'string'
      ? Date.parse(timestamp)
      : Number.NaN;
    const validTimestamp = Number.isFinite(sampleTime) &&
      sampleTime <= now &&
      now - sampleTime <= TELEMETRY_MAX_AGE_MS;

    if (unavailable.has(service) || !validValue || !validTimestamp) {
      missing.push(service);
      continue;
    }

    available.push({
      service,
      value: Number(value.toFixed(P95_DISPLAY_DECIMALS)),
    });
  }

  if (!available.length) {
    return `No fresh P95 latency values are available for comparison. Missing or stale evidence: ${missing.length ? missing.join(', ') : 'all configured services'}.`;
  }

  const highest = Math.max(...available.map(item => item.value));
  const leaders = available.filter(item => item.value === highest);
  const leaderNames = leaders.map(item => item.service).join(', ');
  const result = leaders.length > 1
    ? `Tied highest fresh P95 latency at ${P95_DISPLAY_DECIMALS}-decimal display precision: ${leaderNames} (${highest.toFixed(P95_DISPLAY_DECIMALS)} seconds).`
    : `Highest fresh P95 latency (${P95_DISPLAY_DECIMALS}-decimal display precision): ${leaderNames} (${highest.toFixed(P95_DISPLAY_DECIMALS)} seconds).`;

  return missing.length
    ? `${result} P95 evidence is missing or stale for ${missing.join(', ')}; the overall highest service cannot be confirmed.`
    : result;
}
