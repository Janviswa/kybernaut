import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { Incidents, IncidentDetail } from './pages/Incidents';
import { setToken } from './api';
import { set as setStore, signOut } from './store';

const real = {
  id: 1, incident_id: 'inc-real-1', service: 'kubernetes-lab', incident_type: 'pod_crash',
  severity: 'high', status: 'OPEN', source: 'operational', message: 'Observed pod failure',
  opened_at: '2026-10-27T08:00:00+00:00', source_evidence: null, diagnosis: null,
};
const appIncident = {
  ...real, id: 2, incident_id: 'inc-order-1', service: 'order-service',
  incident_type: 'high_latency', message: 'Order requests are delayed.',
  source_evidence: { simulation: 'local_service_failure' },
};
const json = (body: unknown, status = 200) => ({ ok: status < 300, status, json: async () => body });
function Where() { const l = useLocation(); return <div data-testid="where">{l.pathname + l.search}</div>; }
function renderAt(path: string) {
  return render(<MemoryRouter initialEntries={[path]}><Routes>
    <Route path="/incidents" element={<Incidents />} />
    <Route path="/incidents/:incidentId" element={<IncidentDetail />} />
    <Route path="/console" element={<Where />} />
  </Routes></MemoryRouter>);
}

describe('alert-to-console flow', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    signOut(); setToken('t');
    setStore({ authenticated: true, operatorId: 'op', permissions: ['read'], incidents: { status: 'ready', data: [real, appIncident] as any, error: null } });
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/incidents/inc-real-1')) return json(real);
      if (url.endsWith('/incidents/inc-order-1')) return json(appIncident);
      if (url.endsWith('/incidents/inc-real-1/timeline')) return json({ incident_id: 'inc-real-1', events: [] });
      if (url.endsWith('/incidents/inc-order-1/timeline')) return json({ incident_id: 'inc-order-1', events: [] });
      return json({});
    });
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => { cleanup(); signOut(); vi.unstubAllGlobals(); });

  it('defaults to the four application services and makes Kubernetes history optional', async () => {
    renderAt('/incidents');
    expect(await screen.findByText('inc-order-1')).toBeTruthy();
    expect(screen.queryByText('inc-real-1')).toBeNull();
    expect(screen.getByText(/1 Kubernetes lab incident hidden/)).toBeTruthy();
    expect(screen.getByText('order-service')).toBeTruthy();
    fireEvent.change(screen.getByLabelText('Incident scope'), { target: { value: 'all' } });
    expect(await screen.findByText('inc-real-1')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('source=all'))).toBe(false);
  });

  it('shows the diagnosed incident category and affected workload in the list', async () => {
    setStore({ incidents: { status: 'ready', error: null, data: [{
      ...real,
      incident_type: 'pod_failed',
      source_evidence: {
        type: 'pod_failed',
        resource: 'sim-disk-fill-5dc94cc998-zhnhb',
        resource_kind: 'Pod',
        evidence: ['Evicted: EmptyDir ephemeral storage exceeded its limit'],
      },
    }] as any } });

    renderAt('/incidents');
    fireEvent.change(screen.getByLabelText('Incident scope'), { target: { value: 'all' } });

    expect(await screen.findByText('disk full')).toBeTruthy();
    expect(screen.getByText('Observed: pod failed')).toBeTruthy();
    expect(screen.getByText('sim-disk-fill')).toBeTruthy();
    expect(screen.getByText('Kubernetes lab history · not mapped to an application service')).toBeTruthy();
  });

  it('explains an uninvestigated Kubernetes incident without mislabeling its source', async () => {
    const diskIncident = {
      ...real,
      incident_type: 'pod_failed',
      message: 'Pod evicted after EmptyDir exceeded its limit.',
      source_evidence: {
        source: 'kubernetes',
        type: 'pod_failed',
        namespace: 'kybernaut-lab',
        resource: 'sim-disk-fill-5dc94cc998-zhnhb',
        resource_kind: 'Pod',
        incident_category: 'disk_full',
        recommendation: 'Inspect pod status, events, and previous container logs.',
        evidence: ['Evicted: EmptyDir volume scratch exceeds the limit.'],
      },
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/incidents/inc-real-1')) return json(diskIncident);
      if (url.endsWith('/incidents/inc-real-1/timeline')) {
        return json({ incident_id: 'inc-real-1', events: [] });
      }
      return json({});
    });

    renderAt('/incidents/inc-real-1');

    expect(await screen.findByText('Kubernetes lab observation')).toBeTruthy();
    expect(screen.getByText(/not an incident on one of the four application services/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'View four application services' }).getAttribute('href')).toBe('/incidents');
    expect(screen.getByText('Kubernetes lab workload')).toBeTruthy();
    expect(screen.getByText('disk full · classified from stored evidence')).toBeTruthy();
    expect(screen.getByText('Investigation not run yet')).toBeTruthy();
    expect(screen.getByText(/Start investigation/)).toBeTruthy();
    expect(screen.getAllByText(/Inspect pod status, events/).length).toBeGreaterThan(0);
    expect(screen.queryByRole('link', { name: 'Service health' })).toBeNull();
    expect(await screen.findByText(
      'The backend returned no audit events for this incident.',
    )).toBeTruthy();
  });

  it('shows an empty state instead of generating incidents', async () => {
    setStore({ incidents: { status: 'ready', data: [], error: null } });
    renderAt('/incidents');
    expect(await screen.findByText(/No incidents are currently recorded/)).toBeTruthy();
  });

  it('no longer shows the simulator panel or active-simulated list on Incidents', async () => {
    fetchMock.mockImplementation(async () => json({}));
    renderAt('/incidents');
    await screen.findByText('inc-order-1');
    expect(screen.queryByRole('button', { name: 'Simulate incidents now' })).toBeNull();
    expect(screen.queryByText(/Active simulated incidents/)).toBeNull();
    expect(screen.queryByText('Four-service simulator')).toBeNull();
  });

  it('adds an Open in Console to Investigate button only to unresolved incidents', async () => {
    setStore({ incidents: { status: 'ready', error: null, data: [
      appIncident,
      { ...appIncident, id: 3, incident_id: 'inc-order-2', status: 'RESOLVED' },
    ] as any } });
    renderAt('/incidents');
    await screen.findByText('inc-order-1');
    expect(screen.getByText('inc-order-2')).toBeTruthy();
    const links = screen.getAllByRole('link', { name: 'Open in Console to Investigate' });
    expect(links).toHaveLength(1);
    expect(links[0].getAttribute('href')).toBe('/console?incident=inc-order-1');
  });

  it('hides the Console investigation button on a resolved incident record', async () => {
    const resolved = { ...appIncident, incident_id: 'inc-order-2', status: 'RESOLVED' };
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/incidents/inc-order-2')) return json(resolved);
      if (url.endsWith('/incidents/inc-order-2/timeline')) return json({ incident_id: 'inc-order-2', events: [] });
      return json({});
    });
    renderAt('/incidents/inc-order-2');
    await screen.findByText('Audit timeline');
    expect(screen.queryByRole('link', { name: 'Open in Console to Investigate' })).toBeNull();
  });

  it('opens an alert and navigates to the Console with the exact incident ID', async () => {
    renderAt('/incidents');
    fireEvent.click(await screen.findByText('inc-order-1'));
    const link = await screen.findByRole('link', { name: 'Open in Console to Investigate' });
    expect(link.getAttribute('href')).toBe('/console?incident=inc-order-1');
    fireEvent.click(link);
    expect((await screen.findByTestId('where')).textContent).toBe('/console?incident=inc-order-1');
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/workflow/'))).toBe(false);
  });

  it('has a back button on the incident page', async () => {
    renderAt('/incidents/inc-real-1');
    expect(await screen.findByRole('button', { name: 'Back' })).toBeTruthy();
  });

  it('shows not-found for an unknown incident', async () => {
    fetchMock.mockImplementation(async () => json({ detail: 'Incident not found' }, 404));
    renderAt('/incidents/nope');
    expect(await screen.findByRole('heading', { name: 'Incident not found' })).toBeTruthy();
  });
});