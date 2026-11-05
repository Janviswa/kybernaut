import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import Console from './pages/Console';
import { setToken } from './api';
import { set as setStore, signOut } from './store';

const incident = {
  id: 7, incident_id: 'inc-sim-audit1', service: 'inventory-service', incident_type: 'request_failure',
  severity: 'high', status: 'RESOLVED', source: 'operational',
  message: 'LOCAL SIMULATION: inventory-service requests fail',
  opened_at: '2026-11-04T10:29:17+00:00', resolved_at: '2026-11-04T10:35:11+00:00',
  source_evidence: { simulation: 'local_service_failure' }, diagnosis: null,
};
const events = [
  { event_id: 'a1', timestamp: '2026-11-04T10:35:11+00:00', event_type: 'local_service_simulation_cleared', actor: 'test-operator', outcome: 'resolved', incident_ref: incident.incident_id },
  { event_id: 'a2', timestamp: '2026-11-04T10:35:11+00:00', event_type: 'incident_resolved', actor: 'kybernaut-monitor', incident_ref: incident.incident_id },
];
const response = (body: unknown, status = 200) => ({ ok: status < 300, status, json: async () => body });

describe('Console right-hand Audit tab', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    signOut();
    setToken('test-token');
    setStore({ authenticated: true, operatorId: 'op', permissions: ['read', 'remediation'], incidents: { status: 'idle', data: null, error: null } });
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith(`/incidents/${incident.incident_id}/timeline`)) return response({ incident_id: incident.incident_id, events });
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith('/console/ask')) {
        return response({
          answer: 'inc-sim-audit1 is a **request failure** on **inventory-service**.\n## Cause\n- A deliberate **local simulation** injected the fault.',
          citations: ['incident'], llm_used: false, context_keys: ['incident'], note: '',
        });
      }
      return response({});
    });
    vi.stubGlobal('fetch', fetchMock);
  });
  afterEach(() => { cleanup(); signOut(); vi.unstubAllGlobals(); });

  const open = () => render(
    <MemoryRouter initialEntries={[`/console?incident=${incident.incident_id}`]}>
      <Routes><Route path="/console" element={<Console />} /></Routes>
    </MemoryRouter>);

  it('shows the same full lifecycle flow as the chat audit, not just two raw events', async () => {
    open();
    fireEvent.click(await screen.findByRole('tab', { name: 'Audit' }));
    const flow = await screen.findByLabelText('Audit flow');
    const stages = Array.from(flow.querySelectorAll('.aflow-head strong')).map(node => node.textContent);
    expect(stages).toEqual(['Detected', 'Investigated', 'Approval', 'Executed', 'Verified', 'Resolved']);
    expect(flow.querySelectorAll('.aflow-derived').length).toBeGreaterThan(0);
    expect(flow.textContent).toContain('local service simulation cleared');
  });

  it('answers "what causes the incident?" with a structured, grounded answer', async () => {
    open();
    await screen.findByRole('heading', { name: 'Incident investigation' });
    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), { target: { value: 'what causes the incident?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(await screen.findByText('Cause')).toBeTruthy();
    expect(screen.queryByText('Persisted incident lookup')).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/console/ask'))).toBe(true);
  });
});