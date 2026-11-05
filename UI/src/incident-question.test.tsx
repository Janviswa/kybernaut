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

const incidentId = 'inc-memory-evidence';
const memoryIncident = {
  incident_id: incidentId,
  service: 'inventory-service',
  incident_type: 'memory_leak',
  severity: 'high',
  status: 'RESOLVED',
  diagnosis_status: 'limited',
  message: 'Prometheus detected a memory anomaly.',
  opened_at: '2026-10-28T07:00:00Z',
  updated_at: '2026-10-28T07:01:00Z',
  source_evidence: {
    source: 'prometheus',
    observed_at: '2026-10-28T07:00:00Z',
    metric: {
      name: 'memory_bytes',
      value: 50_962_432,
      baseline_median: 49_991_680,
      deviation_score: 23.07,
      threshold: 5,
      unit: 'bytes',
    },
  },
  diagnosis: {
    root_cause_analysis_status: 'unavailable',
    root_cause_analysis: null,
    policy_recommendation: { action: 'investigate_monitoring' },
    approval_status: 'not_requested',
  },
};

function renderConsole() {
  return render(<MemoryRouter initialEntries={[`/console?incident=${incidentId}`]}>
    <Routes><Route path="/console" element={<Console />} /></Routes>
  </MemoryRouter>);
}

describe('Console evidence-aware incident chat', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let answerCount: number;

  beforeEach(() => {
    signOut();
    setToken('incident-question-test-token');
    setStore({
      authenticated: true,
      operatorId: 'read-only',
      permissions: ['read'],
      incidents: { status: 'ready', data: [], error: null },
    });
    answerCount = 0;
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith(`/incidents/${incidentId}`)) return response(memoryIncident);
      if (url.endsWith(`/incidents/${incidentId}/timeline`)) {
        return response({ incident_id: incidentId, events: [] });
      }
      if (url.endsWith(`/incidents/${incidentId}/ask`)) {
        answerCount += 1;
        const answer = answerCount === 1
          ? 'The inventory-service memory anomaly exceeded its threshold; the cause and fix are not confirmed.'
          : 'No recorded execution confirms a fix.';
        return response({
          incident_id: incidentId,
          answer,
          attachments: [{
            id: 'anomaly-evidence',
            title: 'Recorded anomaly evidence',
            observed_at: '2026-10-28T07:00:00Z',
            content: { value: 50_962_432, baseline_median: 49_991_680 },
          }, {
            id: 'remediation-history',
            title: 'Recorded remediation attempts',
            observed_at: null,
            content: [],
          }],
          missing_evidence: ['confirmed root cause', 'recorded remediation execution'],
          generated_at: '2026-10-28T07:02:00Z',
        });
      }
      if (url.endsWith('/telemetry')) {
        return response({
          observed_at: '2026-10-28T07:00:00Z',
          services: { 'inventory-service': { memory_bytes: 50_962_432 } },
          unavailable_services: [],
        });
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

  it('answers memory questions from the selected persisted incident with evidence attachments', async () => {
    renderConsole();
    await screen.findByText('Persisted incident record loaded.');
    const input = screen.getByRole('textbox', { name: 'Ask Kybernaut' });
    fireEvent.change(input, {
      target: { value: 'How did the memory leak happen, and how was it fixed?' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect((await screen.findAllByText(/the cause and fix are not confirmed/)).length)
      .toBeGreaterThan(0);
    expect(screen.getByText('Recorded anomaly evidence')).toBeTruthy();
    expect(screen.getByText('Recorded remediation attempts')).toBeTruthy();
    expect(screen.getByText(/Evidence not available: confirmed root cause, recorded remediation execution/)).toBeTruthy();
    const details = screen.getByText('Recorded anomaly evidence').closest('details');
    expect(details).toBeTruthy();
    fireEvent.click(screen.getByText('Recorded anomaly evidence'));
    expect(details?.textContent).toContain('50962432');
    expect(details?.textContent).toContain('49991680');
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/telemetry'))).toBe(false);
    const askCall = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith(`/incidents/${incidentId}/ask`),
    );
    expect(askCall?.[1]?.method).toBe('POST');
    expect(new Headers(askCall?.[1]?.headers).get('Authorization')).toMatch(/^Bearer\s+/);
  });

  it('retains the active incident context and previous turns for follow-up questions', async () => {
    renderConsole();
    await screen.findByText('Persisted incident record loaded.');
    const input = screen.getByRole('textbox', { name: 'Ask Kybernaut' });
    fireEvent.change(input, { target: { value: 'What happened?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    await screen.findAllByText(/the cause and fix are not confirmed/);

    fireEvent.change(input, { target: { value: 'How was it fixed?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect((await screen.findAllByText('No recorded execution confirms a fix.')).length)
      .toBeGreaterThan(0);

    const requests = fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith(`/incidents/${incidentId}/ask`),
    );
    expect(requests).toHaveLength(2);
    const secondBody = JSON.parse(String(requests[1][1]?.body));
    expect(secondBody.question).toBe('How was it fixed?');
    expect(secondBody.history).toEqual([
      { role: 'user', content: 'What happened?' },
      {
        role: 'assistant',
        content: 'The inventory-service memory anomaly exceeded its threshold; the cause and fix are not confirmed.',
      },
    ]);
  });
});
