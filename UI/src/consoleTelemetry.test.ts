import { describe, expect, it } from 'vitest';
import { selectTelemetryServices, summarizeHighestP95 } from './consoleTelemetry';

const timestamp = new Date().toISOString();
const row = (p95: number) => ({
  p95_latency_seconds: p95,
  sample_timestamps: { p95_latency_seconds: timestamp },
});

const telemetry = (services: Record<string, unknown>, unavailable: string[] = []) => ({
  source: 'prometheus' as const,
  observed_at: timestamp,
  services,
  unavailable_services: unavailable,
});

describe('Console telemetry result handling', () => {
  it('filters a service-specific request in the structured result and retains evidence timestamps', () => {
    const original = telemetry({
      'order-service': row(0.1),
      'payment-service': row(0.2),
      'user-service': row(0.3),
      'inventory-service': row(0.4),
    });

    const result = selectTelemetryServices(original, 'payment-service');

    expect(Object.keys(result.services)).toEqual(['payment-service']);
    expect(result.services['payment-service']).toEqual(row(0.2));
    expect(result.observed_at).toBe(timestamp);
    expect(original.services).toHaveProperty('order-service');
  });

  it('preserves all service rows when no service is requested', () => {
    const original = telemetry({
      'order-service': row(0.1),
      'payment-service': row(0.2),
    });

    expect(selectTelemetryServices(original).services).toEqual(original.services);
  });

  it('selects the highest fresh P95 value and identifies ties', () => {
    const result = summarizeHighestP95(telemetry({
      'order-service': row(0.2),
      'payment-service': row(0.5001),
      'user-service': row(0.5004),
      'inventory-service': row(0.1),
    }));

    expect(result).toBe(
      'Tied highest fresh P95 latency at 3-decimal display precision: payment-service, user-service (0.500 seconds).',
    );
  });

  it('does not compare missing, invalid, or backend-reported stale P95 evidence', () => {
    expect(summarizeHighestP95(telemetry({
      'order-service': row(0.1),
      'payment-service': row(0.9),
      'user-service': null,
      'inventory-service': row(0.2),
    }, ['payment-service', 'user-service', 'inventory-service']), Date.parse(timestamp) + 1_000)).toContain(
      'Highest fresh P95 latency (3-decimal display precision): order-service (0.100 seconds). P95 evidence is missing or stale for payment-service, user-service, inventory-service',
    );

    expect(summarizeHighestP95(telemetry({
      'order-service': {
        p95_latency_seconds: 99,
        sample_timestamps: { p95_latency_seconds: '2020-01-01T00:00:00.000Z' },
      },
      'payment-service': null,
      'user-service': null,
      'inventory-service': null,
    }, ['payment-service', 'user-service', 'inventory-service']))).toContain(
      'No fresh P95 latency values are available for comparison.',
    );
  });
});
