import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { Services } from './pages/Services';
import { setToken } from './api';
import { set as setStore, signOut } from './store';

const response = (body: unknown) => ({
  ok: true,
  status: 200,
  json: async () => body,
});

const simulations = [
  {
    incident_id: 'inc-order-sim',
    service: 'order-service',
    incident_type: 'high_latency',
    severity: 'medium',
    status: 'OPEN',
    message: 'Order requests are delayed by two seconds.',
    source_evidence: { simulation: 'local_service_failure' },
  },
  {
    incident_id: 'inc-payment-sim',
    service: 'payment-service',
    incident_type: 'http_5xx',
    severity: 'high',
    status: 'OPEN',
    message: 'Payment requests return HTTP 503.',
    source_evidence: { simulation: 'local_service_failure' },
  },
  {
    incident_id: 'inc-user-sim',
    service: 'user-service',
    incident_type: 'service_unavailable',
    severity: 'critical',
    status: 'OPEN',
    message: 'User requests return HTTP 503.',
    source_evidence: { simulation: 'local_service_failure' },
  },
  {
    incident_id: 'inc-inventory-sim',
    service: 'inventory-service',
    incident_type: 'request_failure',
    severity: 'high',
    status: 'OPEN',
    message: 'Inventory requests return HTTP 500.',
    source_evidence: { simulation: 'local_service_failure' },
  },
];

describe('Service Health incident status', () => {
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
      services: {
        status: 'ready',
        error: null,
        data: ['order-service', 'payment-service', 'user-service', 'inventory-service']
          .map(service => ({
            service,
            status: 'ready' as const,
            data: { service, status: 'up', findings: [], evidence: {} },
          })),
      },
      incidents: { status: 'ready', error: null, data: simulations as any },
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/local-service-simulation')) {
        return response({
          enabled: true,
          active_services: ['order-service', 'payment-service', 'user-service', 'inventory-service'],
          interval_seconds: 600,
        });
      }
      if (url.endsWith('/telemetry')) {
        return response({
          source: 'prometheus',
          observed_at: '2026-01-01T00:00:00Z',
          services: {},
          unavailable_services: [],
        });
      }
      if (url.includes('/incidents/history')) {
        return response({ incidents: simulations });
      }
      if (url.includes('/diagnose/')) {
        const service = decodeURIComponent(url.split('/diagnose/')[1]);
        return response({ service, status: 'up', findings: [], evidence: {} });
      }
      return response({});
    }));
  });

  afterEach(() => {
    cleanup();
    signOut();
    vi.unstubAllGlobals();
  });

  it('shows Prometheus diagnosis separately from unresolved incident evidence', async () => {
    const { container } = render(<MemoryRouter><Services /></MemoryRouter>);

    await screen.findByText('Order requests are delayed by two seconds.');
    const statuses = Array.from(container.querySelectorAll('.badge')).map(
      badge => badge.textContent,
    );
    expect(statuses).toEqual(['up', 'up', 'up', 'up']);
    expect(screen.getByText('Order requests are delayed by two seconds.')).toBeTruthy();
    expect(screen.getByText('Payment requests return HTTP 503.')).toBeTruthy();
    expect(screen.getByText('User requests return HTTP 503.')).toBeTruthy();
    expect(screen.getByText('Inventory requests return HTTP 500.')).toBeTruthy();
    expect(screen.queryByText(/simulat/i)).toBeNull();
    expect(screen.queryByText('Four-service incident simulator')).toBeNull();
    expect(screen.queryByText(/Runs a different local failure/)).toBeNull();
  });
});