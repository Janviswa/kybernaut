import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import Reliability from './pages/Reliability';
import { setToken } from './api';
import { set as setStore, signOut } from './store';

const response = (body: unknown) => ({
  ok: true,
  status: 200,
  json: async () => body,
});
describe('Reliability & Cost', () => {
  beforeEach(() => {
    signOut();
    setToken('test-token');
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    setStore({
      authenticated: true,
      operatorId: 'operator',
      permissions: ['read'],
      metrics: {
        status: 'ready',
        error: null,
        data: {
          total_incidents: 778,
          active_incidents: 31,
          mttr_seconds: 3600,
          recurrence_rate: 0.15,
          by_status: { RESOLVED: 778 },
          by_severity: { high: 778 },
        },
      },
      incidents: {
        status: 'ready',
        error: null,
        data: [{
          incident_id: 'inc-history',
          service: 'kubernetes-lab',
          status: 'OPEN',
        }],
      },
      cost: {
        status: 'ready',
        error: null,
        data: {
          provenance: 'deterministic synthetic weekly data; no monitoring source',
          pricing: { is_example: true, source: 'EXAMPLE VALUES ONLY' },
          recommendations: [{
            resource: 'worker-underused',
            current_monthly_cost: 180,
            recommended_monthly_cost: 95,
            potential_monthly_savings: 85,
          }],
          summary: { potential_monthly_savings: 85 },
        },
      },
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/cost/usage')) {
        return response({
          source: 'prometheus',
          observed_at: '2026-11-02T15:00:00Z',
          currency: 'USD',
          status: 'rates_not_configured',
          pricing: {
            configured: false,
            configuration: [
              'KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD',
              'KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD',
            ],
          },
          services: {
            'order-service': {
              cpu_cores_used: 0.125,
              memory_bytes: 104857600,
              request_rate: 2.5,
              p95_latency_seconds: 0.04,
              estimated_hourly_cost_usd: null,
              estimated_monthly_run_rate_usd: null,
            },
          },
          summary: {
            estimated_hourly_cost_usd: null,
            estimated_monthly_run_rate_usd: null,
            month_hours: 730,
            all_services_available: false,
          },
          unavailable_services: ['payment-service', 'user-service', 'inventory-service'],
          disclaimer: 'Not an invoice.',
        });
      }
      return response({});
    }));
  });

  afterEach(() => {
    cleanup();
    signOut();
    vi.unstubAllGlobals();
  });

  it('distinguishes four-service incidents and uses Prometheus instead of the saved weekly report', async () => {
    render(<MemoryRouter><Reliability /></MemoryRouter>);

    expect(await screen.findByText('Live service usage and estimated cost')).toBeTruthy();
    expect(screen.getByText('0.125')).toBeTruthy();
    expect(screen.getByText('100.0')).toBeTruthy();
    expect(await screen.findByRole('button', {
      name: /Active incidents · four services\s+0/,
    })).toBeTruthy();
    expect(screen.getByText(/31 active incidents across all sources/)).toBeTruthy();
    expect(screen.getByText('Active incidents · four services')).toBeTruthy();
    expect(screen.getByText('Live Prometheus cost estimate')).toBeTruthy();
    expect(screen.queryByText(/synthetic/i)).toBeNull();
    expect(screen.getByText('Where can we cut?')).toBeTruthy();
    expect(screen.getByText('Highest current CPU usage')).toBeTruthy();
    expect(screen.getByText('order-service · 0.125 cores')).toBeTruthy();
    expect(screen.getByText('Highest current memory usage')).toBeTruthy();
    expect(screen.getByText('order-service · 100.0 MiB')).toBeTruthy();
    expect(screen.queryByText('180')).toBeNull();
    expect(screen.queryByText('85')).toBeNull();
    expect(screen.getByText(/Live monetary estimates are unavailable/)).toBeTruthy();
  });

  it('uses configured USD rates to display the point-in-time run rate', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/cost/usage')) {
        return response({
          source: 'prometheus',
          observed_at: '2026-11-02T15:00:00Z',
          currency: 'USD',
          status: 'estimated',
          pricing: {
            configured: true,
            cpu_per_core_hour_usd: 10,
            memory_per_gib_hour_usd: 5.25,
            source: 'illustrative owner-configured internal rates; not actual cloud-provider costs',
          },
          services: {
            'order-service': {
              cpu_cores_used: 0.4,
              memory_bytes: 1024 ** 3,
              request_rate: 3.2,
              p95_latency_seconds: 0.12,
              estimated_hourly_cost_usd: 9.25,
              estimated_monthly_run_rate_usd: 6752.5,
              estimated_cpu_monthly_cost_usd: 2920,
              estimated_memory_monthly_cost_usd: 3832.5,
              illustrative_10_percent_cpu_reduction_monthly_savings_usd: 292,
              illustrative_10_percent_memory_reduction_monthly_savings_usd: 383.25,
            },
            'payment-service': null,
            'user-service': null,
            'inventory-service': null,
          },
          summary: {
            estimated_hourly_cost_usd: null,
            estimated_monthly_run_rate_usd: null,
            month_hours: 730,
            all_services_available: false,
          },
          unavailable_services: ['payment-service', 'user-service', 'inventory-service'],
          disclaimer: 'Not an invoice.',
        });
      }
      return response({});
    }));

    render(<MemoryRouter><Reliability /></MemoryRouter>);

    expect(await screen.findByText(/Some Prometheus usage is unavailable/)).toBeTruthy();
    expect(screen.getByText('$9.25')).toBeTruthy();
    expect(screen.getByText('$6,752.50')).toBeTruthy();
    expect(screen.getByText(/\$10\.00 per CPU core-hour \+ \$5\.25 per GiB-hour/)).toBeTruthy();
    expect(screen.getByText(/Largest current cost component: order-service memory/)).toBeTruthy();
    expect(screen.getByText(/If its current measured usage were 10% lower, the approximate run-rate reduction would be \$383\.25\/month/)).toBeTruthy();
  });

});
