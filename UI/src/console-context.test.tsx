import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import Console from './pages/Console';
import { setToken } from './api';
import { set as setStore, signOut } from './store';

const incident = {
  id: 9,
  incident_id: 'inc-ctx-1',
  service: 'user-service',
  incident_type: 'service_unavailable',
  severity: 'critical',
  status: 'OPEN',
  source: 'operational',
  message: 'LOCAL SIMULATION: user-service — User requests return HTTP 503.',
  opened_at: '2026-11-03T05:00:00+00:00',
  source_evidence: { simulation: 'local_service_failure', evidence: ['The local user service returned HTTP 503 while its fault was active.'] },
  diagnosis: null,
};

const response = (body: unknown, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });

const healthFor = (service: string) => ({
  service, health_status: 'healthy', freshness_status: 'fresh', findings: [], checked_at: new Date().toISOString(),
});

function renderConsole() {
  return render(
    <MemoryRouter initialEntries={[`/console?incident=${incident.incident_id}`]}>
      <Routes><Route path="/console" element={<Console />} /></Routes>
    </MemoryRouter>,
  );
}

const ask = (value: string) => {
  fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), { target: { value } });
  fireEvent.click(screen.getByRole('button', { name: 'Send' }));
};

describe('Console follow-ups stay on the open incident', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    signOut();
    setToken('test-token');
    setStore({ authenticated: true, operatorId: 'op', permissions: ['read', 'remediation'], incidents: { status: 'idle', data: null, error: null } });
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const health = url.match(/\/service-health\/([a-z-]+)$/);
      if (health) return response(healthFor(health[1]));
      if (url.endsWith('/services')) return response({ services: ['order-service', 'payment-service', 'user-service', 'inventory-service'] });
      if (url.endsWith('/telemetry')) {
        return response({ services: { 'user-service': { cpu_cores_used: 0.01, memory_bytes: 52428800, request_rate: 1.5, p95_latency_seconds: 0.12 } }, unavailable_services: [] });
      }
      if (url.includes('/audit?')) {
        return response({ events: [
          { event_id: 'e2', timestamp: '2026-11-03T05:02:00+00:00', event_type: 'remediation_executed', actor: 'op', incident_ref: incident.incident_id, outcome: 'success' },
          { event_id: 'e1', timestamp: '2026-11-03T05:00:00+00:00', event_type: 'incident_opened', actor: 'simulator', incident_ref: incident.incident_id },
        ] });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith('/timeline')) return response({ incident_id: incident.incident_id, events: [] });
      return response({});
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => { cleanup(); signOut(); vi.unstubAllGlobals(); });

  it('"check the service health" explains only the service that has the incident', async () => {
    renderConsole();
    await screen.findByRole('heading', { name: 'Incident investigation' });
    ask('now check the service health');

    expect(await screen.findByLabelText('Service health for user-service')).toBeTruthy();
    const healthCalls = fetchMock.mock.calls.map(([url]) => String(url)).filter(url => url.includes('/service-health/'));
    expect(healthCalls).toHaveLength(1);
    expect(healthCalls[0]).toContain('/service-health/user-service');
    expect(screen.getByText('Incident on this service')).toBeTruthy();
  });

  it('the Service health button answers inside the console without leaving the page', async () => {
    renderConsole();
    await screen.findByRole('heading', { name: 'Incident investigation' });
    fireEvent.click(screen.getAllByRole('button', { name: /Service health/ })[0]);

    expect(await screen.findByLabelText('Service health for user-service')).toBeTruthy();
    expect(screen.queryByRole('link', { name: /Service health/ })).toBeNull();
  });

  it('"show the audit" draws the audit flow for the open incident, oldest first', async () => {
    renderConsole();
    await screen.findByRole('heading', { name: 'Incident investigation' });
    ask('show the audit');

    const flow = await screen.findByLabelText('Audit flow');
    const titles = Array.from(flow.querySelectorAll('.aflow-node b')).map(node => node.textContent);
    expect(titles).toEqual(['incident opened', 'remediation executed']);
    const auditCall = fetchMock.mock.calls.map(([url]) => String(url)).find(url => url.includes('/audit?'));
    expect(auditCall).toContain('incident_id=inc-ctx-1');
  });
});