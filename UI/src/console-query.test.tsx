import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import Console from './pages/Console';
import { setToken } from './api';
import { set as setStore, signOut } from './store';

const response = (body: unknown, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

function renderConsole() {
  return render(<MemoryRouter initialEntries={['/console']}>
    <Routes><Route path="/console" element={<Console />} /></Routes>
  </MemoryRouter>);
}

describe('Console natural-language queries', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    signOut();
    setToken('console-query-test-token');
    setStore({
      authenticated: true,
      operatorId: 'read-only',
      permissions: ['read'],
      incidents: { status: 'ready', data: [], error: null },
    });
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/service-health/')) {
        const service = decodeURIComponent(url.split('/service-health/')[1]);
        return response({
          service,
          health_status: 'healthy',
          freshness_status: 'fresh',
          evidence_status: 'available',
          evidence_timestamps: { service_availability: { timestamp: '2026-10-27T13:00:00+00:00' } },
          findings: [{ type: 'no_detected_anomaly' }],
        });
      }
      if (url.endsWith('/telemetry')) {
        return response({
          source: 'prometheus',
          observed_at: '2026-10-27T13:01:00+00:00',
          services: {
            'order-service': {
              cpu_cores_used: 0.1,
              memory_bytes: 1024,
              request_rate: 1.5,
              p95_latency_seconds: 0.1,
              observed_at: '2026-10-27T13:00:58+00:00',
              sample_timestamps: {
                cpu_cores_used: '2026-10-27T13:00:58+00:00',
                memory_bytes: '2026-10-27T13:00:58+00:00',
                request_rate: '2026-10-27T13:00:58+00:00',
                p95_latency_seconds: '2026-10-27T13:00:58+00:00',
              },
            },
            'payment-service': {
              cpu_cores_used: 0.4,
              memory_bytes: 4096,
              request_rate: 2.5,
              p95_latency_seconds: 0.2,
              observed_at: '2026-10-27T13:00:58+00:00',
              sample_timestamps: {
                cpu_cores_used: '2026-10-27T13:00:58+00:00',
                memory_bytes: '2026-10-27T13:00:58+00:00',
                request_rate: '2026-10-27T13:00:58+00:00',
                p95_latency_seconds: '2026-10-27T13:00:58+00:00',
              },
            },
            'user-service': {
              cpu_cores_used: 0.3,
              memory_bytes: 3072,
              request_rate: 3.5,
              p95_latency_seconds: 0.3,
              observed_at: '2026-10-27T13:00:58+00:00',
              sample_timestamps: {
                cpu_cores_used: '2026-10-27T13:00:58+00:00',
                memory_bytes: '2026-10-27T13:00:58+00:00',
                request_rate: '2026-10-27T13:00:58+00:00',
                p95_latency_seconds: '2026-10-27T13:00:58+00:00',
              },
            },
            'inventory-service': {
              cpu_cores_used: 0.2,
              memory_bytes: 2048,
              request_rate: 2,
              p95_latency_seconds: 0.15,
              observed_at: '2026-10-27T13:00:58+00:00',
              sample_timestamps: {
                cpu_cores_used: '2026-10-27T13:00:58+00:00',
                memory_bytes: '2026-10-27T13:00:58+00:00',
                request_rate: '2026-10-27T13:00:58+00:00',
                p95_latency_seconds: '2026-10-27T13:00:58+00:00',
              },
            },
          },
          unavailable_services: [],
        });
      }
      if (url.endsWith('/services')) {
        return response({ services: [
          'order-service',
          'payment-service',
          'user-service',
          'inventory-service',
        ] });
      }
      if (url.includes('/incidents/history?source=demo')) {
        return response({ incidents: [] });
      }
      if (url.includes('/audit?')) {
        return response({ events: Array.from({ length: 10 }, (_, index) => ({
          timestamp: `2026-10-27T13:${String(9 - index).padStart(2, '0')}:00+00:00`,
          event_type: index === 0 ? 'policy_evaluated' : `audit_${index}`,
          actor: 'operator-1',
          resource: `resource-${index}`,
          outcome: 'allowed',
          incident_ref: 'inc-123',
          event_id: `event-${index}`,
        })) });
      }
      if (url.endsWith('/incidents/inc-missing')) {
        return response({ detail: 'Incident not found' }, 404);
      }
      return response({});
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    cleanup();
    signOut();
    vi.unstubAllGlobals();
  });

  it('runs all live service diagnoses on Enter and labels results from evidence', async () => {
    renderConsole();
    const input = screen.getByRole('textbox', { name: 'Ask Kybernaut' });
    fireEvent.change(input, { target: { value: 'Check the health of all services' } });
    fireEvent.keyDown(input, { key: 'Enter', shiftKey: false });

    expect(await screen.findAllByText('HEALTHY')).toHaveLength(4);
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).includes('/service-health/'));
    expect(calls).toHaveLength(4);
    for (const [, init] of calls) {
      expect(new Headers((init as RequestInit).headers).get('Authorization'))
        .toBe('Bearer console-query-test-token');
    }
  });

  it('returns live CPU and memory data with its observation timestamp', async () => {
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Check CPU and memory usage for payment service' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('0.4')).toBeTruthy();
    expect(screen.getByText('4096')).toBeTruthy();
    expect(screen.getAllByText(/2026-10-27T13:00:58\+00:00/).length).toBeGreaterThan(0);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/telemetry')))
      .toBe(true);
  });

  it('returns only the requested service in current telemetry', async () => {
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Show current telemetry for payment-service only' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    const table = await screen.findByRole('table');
    const rows = [...table.querySelectorAll('tbody tr')];
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain('payment-service');
    expect(rows[0].textContent).not.toContain('order-service');
  });

  it('keeps all service rows for an explicit all-services telemetry request', async () => {
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Show current telemetry for all services' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    const table = await screen.findByRole('table');
    expect(table.querySelectorAll('tbody tr')).toHaveLength(4);
  });

  it('reports the highest P95 service and states when other services are unavailable', async () => {
    const observedAt = new Date().toISOString();
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/telemetry')) {
        return response({
          source: 'prometheus',
          observed_at: observedAt,
          services: {
            'order-service': {
              p95_latency_seconds: 0.2,
              sample_timestamps: { p95_latency_seconds: observedAt },
            },
            'payment-service': {
              p95_latency_seconds: 0.5,
              sample_timestamps: { p95_latency_seconds: observedAt },
            },
            'user-service': null,
            'inventory-service': null,
          },
          unavailable_services: ['user-service', 'inventory-service'],
        });
      }
      return response({});
    });

    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Which service currently has the highest P95 latency?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText(/Highest fresh P95 latency \(3-decimal display precision\): payment-service \(0.500 seconds\)/)).toBeTruthy();
    expect(screen.getByText(/overall highest service cannot be confirmed/)).toBeTruthy();
  });

  it('does not render audit events without an exact incident association', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes('/audit?')) {
        return response({ events: [{
          timestamp: '2026-10-27T13:09:00+00:00',
          event_type: 'unrelated_event',
          actor: 'operator-1',
          incident_ref: 'inc-other',
          event_id: 'unrelated-event',
        }] });
      }
      return response({});
    });

    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Show audit trail for incident inc-123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText(/No audit events were found for incident inc-123/)).toBeTruthy();
    expect(screen.queryByText(/unrelated_event/)).toBeNull();
  });

  it('does not replace an unknown incident audit filter with general history', async () => {
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Show audit trail for incident invalid-123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText(/No audit events were found for incident invalid-123/)).toBeTruthy();
    const requests = fetchMock.mock.calls
      .map(([url]) => String(url))
      .filter(url => url.includes('/audit?'));
    expect(requests).toHaveLength(1);
    expect(requests[0]).toContain('incident_id=invalid-123');
    expect(requests[0]).toContain('source=operational');
  });

  it('explains when returned audit records lack an incident association', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes('/audit?')) {
        return response({ events: [{
          timestamp: '2026-10-27T13:09:00+00:00',
          event_type: 'unassociated_event',
          actor: 'operator-1',
          event_id: 'unassociated-event',
        }] });
      }
      return response({});
    });

    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Show audit trail for incident inc-123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText(/lacks the incident reference needed to associate it/)).toBeTruthy();
    expect(screen.getByRole('table').textContent).not.toContain('unassociated_event');
  });

  it('queries the authenticated audit API for the requested incident', async () => {
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Show audit trail for incident inc-123' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect((await screen.findAllByText(/policy_evaluated/)).length).toBeGreaterThan(0);
    expect(fetchMock.mock.calls.some(([url, init]) =>
      String(url).includes('/audit?') &&
      String(url).includes('incident_id=inc-123') &&
      String(url).includes('limit=5') &&
      new Headers((init as RequestInit).headers).has('Authorization'),
    )).toBe(true);
  });

  it('limits requested audit history before rendering and sorts by timestamp', async () => {
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Last 5 audit only' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText(/Showing 5 of up to 5 requested/)).toBeTruthy();
    const tableRows = screen.getByRole('table').querySelectorAll('tbody tr');
    expect(tableRows).toHaveLength(5);
    expect(tableRows[0].textContent).toContain('13:09:00');
    expect(tableRows[4].textContent).toContain('13:05:00');
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).includes('/audit?') &&
      String(url).includes('limit=5') &&
      String(url).includes('source=operational'),
    )).toBe(true);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/service-health/')))
      .toBe(false);
  });

  it('defaults unspecified audit-history requests to five records', async () => {
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Show the latest audit events' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText(/Showing 5 of up to 5 requested/)).toBeTruthy();
    expect(screen.getByRole('table').querySelectorAll('tbody tr')).toHaveLength(5);
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).includes('/audit?') &&
      String(url).includes('limit=5') &&
      String(url).includes('source=operational'),
    )).toBe(true);
  });

  it('lists configured services without issuing health requests', async () => {
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'What services are available?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('order-service')).toBeTruthy();
    expect(screen.getByText('4 configured services. No health checks were run.')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/api/services')))
      .toBe(true);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/service-health/')))
      .toBe(false);
  });

  it('keeps stale health evidence unknown rather than healthy', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/service-health/')) {
        const service = decodeURIComponent(url.split('/service-health/')[1]);
        return response({
          service,
          health_status: 'unknown',
          freshness_status: 'stale',
          findings: [],
        });
      }
      if (url.endsWith('/services')) {
        return response({ services: [
          'order-service',
          'payment-service',
          'user-service',
          'inventory-service',
        ] });
      }
      return response({});
    });

    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Check the health of order service' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('STALE')).toBeTruthy();
    expect(screen.queryByText('HEALTHY')).toBeNull();
  });

  it('reports an unknown incident accurately and does not run workflows', async () => {
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'What caused incident inc-missing?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect((await screen.findByRole('alert')).textContent)
      .toContain('Incident not found in persisted records.');
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/workflow/')))
      .toBe(false);
  });

  const askReply = (answer: string) => async (input: RequestInfo | URL) => {
    if (String(input).endsWith('/console/ask')) {
      return response({ answer, citations: ['service_health'], llm_used: true, insufficient_data: false, context_keys: ['service_health'], note: '' });
    }
    return response({});
  };

  it('answers an ambiguous service mention from stored records instead of guessing', async () => {
    fetchMock.mockImplementation(askReply('payment-service: status up, evidence available.'));
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'payment service' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('payment-service: status up, evidence available.')).toBeTruthy();
    const ask = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/console/ask'));
    expect(JSON.parse(String((ask?.[1] as RequestInit).body)).question).toBe('payment service');
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).includes('/service-health/') || String(url).endsWith('/telemetry'),
    )).toBe(false);
  });

  it('sends unsupported questions (such as log searches) to the grounded assistant, which cannot invent logs', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/console/ask')) {
        return response({ answer: 'The stored records contain no application logs.', citations: [], llm_used: true, insufficient_data: true, context_keys: [], note: '' });
      }
      return response({});
    });
    renderConsole();
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Search application logs for timeouts' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('The stored records contain no application logs.')).toBeTruthy();
    expect(screen.getByText(/do not contain enough/)).toBeTruthy();
    expect(fetchMock.mock.calls.every(([url]) => String(url).endsWith('/console/ask'))).toBe(true);
  });
});