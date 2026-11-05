import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import Console from './pages/Console';
import { Investigations } from './pages/Investigations';
import { AuditFlowCard } from './consoleCards';
import { setToken } from './api';
import { set as setStore, signOut } from './store';

const incident = {
  id: 7,
  incident_id: 'inc-test-123',
  service: 'user-service',
  incident_type: 'cpu_spike',
  severity: 'medium',
  status: 'OPEN',
  source: 'operational',
  message: 'Prometheus anomaly: cpu spike observed.',
  opened_at: '2026-10-27T08:00:00+00:00',
  diagnosis_status: 'completed',
  source_evidence: {
    scenario: 'CpuSaturation',
    created_at: '2026-10-27T08:00:00+00:00',
    workload_reference: null,
  },
  diagnosis: {
    root_cause_analysis_status: 'unavailable',
    policy_recommendation: { action: 'investigate_monitoring' },
  },
};

const response = (body: unknown, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
  clone: () => ({ json: async () => body }),
});

describe('AuditFlowCard', () => {
  afterEach(cleanup);

  it('shows the completed lifecycle for a successfully cleared local simulation', () => {
    render(<AuditFlowCard incidentId="inc-local-user" events={[
      {
        timestamp: '2026-10-27T08:01:00+00:00',
        event_type: 'incident_resolved',
        actor: 'kybernaut-monitor',
        outcome: 'local_simulation_cleared',
      },
      {
        timestamp: '2026-10-27T08:02:00+00:00',
        event_type: 'local_service_simulation_cleared',
        actor: 'operator',
        outcome: 'resolved',
      },
    ]} />);

    const phases = screen.getByLabelText('Lifecycle stages');
    expect(phases.querySelectorAll('li.on')).toHaveLength(6);
    expect(phases.textContent).toContain(
      'DetectedInvestigatedApprovalExecutedVerifiedResolved',
    );
  });

  it('lists every lifecycle stage as its own step, labelling derived ones', () => {
    render(<AuditFlowCard incidentId="inc-local-user" events={[
      { timestamp: '2026-10-27T08:02:00+00:00', event_type: 'local_service_simulation_cleared', actor: 'operator', outcome: 'resolved' },
      { timestamp: '2026-10-27T08:02:00+00:00', event_type: 'incident_resolved', actor: 'monitor', outcome: 'resolved' },
    ]} />);
    const stages = document.querySelectorAll('.aflow-stage');
    expect(stages).toHaveLength(6);
    expect(document.querySelectorAll('.aflow-stage.is-on')).toHaveLength(6);
    expect(document.querySelectorAll('.aflow-derived')).toHaveLength(4);
    expect(document.querySelectorAll('.aflow-node')).toHaveLength(2);
  });

  it('shows unreached stages as pending', () => {
    render(<AuditFlowCard incidentId="x" events={[
      { timestamp: '2026-10-27T08:01:00+00:00', event_type: 'incident_detected', outcome: 'detected' },
    ]} />);
    expect(document.querySelectorAll('.aflow-stage.is-off')).toHaveLength(5);
  });

  it('does not mark investigation or action stages complete for a detection alone', () => {
    render(<AuditFlowCard incidentId="inc-local-user" events={[{
      timestamp: '2026-10-27T08:01:00+00:00',
      event_type: 'incident_detected',
      actor: 'kybernaut-monitor',
      outcome: 'detected',
    }]} />);

    const phases = screen.getByLabelText('Lifecycle stages');
    expect(phases.querySelectorAll('li.on')).toHaveLength(1);
    expect(phases.textContent).toContain('Detected');
  });
});

function renderConsole(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/console" element={<Console />} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderInvestigationHistory() {
  return render(
    <MemoryRouter initialEntries={['/investigations']}>
      <Routes>
        <Route path="/investigations" element={<Investigations />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe('Console incident investigation', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    signOut();
    setToken('test-token');
    setStore({
      authenticated: true,
      operatorId: 'read-only',
      permissions: ['read'],
      incidents: { status: 'idle', data: null, error: null },
    });
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith(`/incidents/${incident.incident_id}`)) {
        return response(incident);
      }
      if (url.includes('/incidents/history')) {
        return response({ incidents: [] });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}/timeline`)) {
        return response({
          incident_id: incident.incident_id,
          events: [{
            timestamp: '2026-10-27T08:01:00+00:00',
            event_type: 'diagnosis_completed',
            actor: 'kybernaut-demo-scheduler',
            outcome: 'completed',
          }],
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

  it('loads persisted history and opens the selected incident in Console', async () => {
    renderConsole(`/console?incident=${encodeURIComponent(incident.incident_id)}`);

    expect(await screen.findByRole('heading', { name: 'Incident investigation' }))
      .toBeTruthy();
    expect((await screen.findAllByText('CpuSaturation')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('investigate_monitoring').length).toBeGreaterThan(0);
    expect(screen.getByText(/Opening it does not rerun analysis/)).toBeTruthy();

    fireEvent.click(screen.getByRole('tab', { name: 'Audit' }));
    expect(await screen.findByText(/diagnosis completed/i)).toBeTruthy();

    expect(screen.getAllByRole('link', { name: /Open incident record/ })[0]
      .getAttribute('href')).toBe(`/incidents/${incident.incident_id}`);
    const urls = fetchMock.mock.calls.map(([url]) => String(url));
    expect(urls.some(url => url.endsWith(`/incidents/${incident.incident_id}`))).toBe(true);
    expect(urls.some(url => url.endsWith(`/incidents/${incident.incident_id}/timeline`))).toBe(true);
    expect(urls.some(url => url.includes('/workflow/'))).toBe(false);
  });

  it('explains why an incident has no saved diagnosis and shows its recorded trigger', async () => {
    const unassessedIncident = {
      ...incident,
      diagnosis_status: null,
      diagnosis: null,
      message: 'LOCAL SIMULATION: user-service requests return HTTP 503.',
      source_evidence: {
        simulation: 'local_service_failure',
        description: 'User requests return HTTP 503.',
        evidence: ['The local user service returned HTTP 503 while its fault was active.'],
      },
    };
    fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith(`/incidents/${incident.incident_id}`)) {
        return response(unassessedIncident);
      }
      if (url.includes('/incidents/history')) return response({ incidents: [] });
      if (url.endsWith(`/incidents/${incident.incident_id}/timeline`)) {
        return response({ incident_id: incident.incident_id, events: [] });
      }
      return response({});
    });
    vi.stubGlobal('fetch', fetchMock);

    renderConsole(`/console?incident=${incident.incident_id}`);

    expect(await screen.findAllByText(
      'Recorded trigger: User requests return HTTP 503.',
    )).toHaveLength(2);
    expect(screen.getAllByText(
      'Recorded trigger: User requests return HTTP 503.',
    )).toHaveLength(2);
    expect(screen.queryByText(/not established from available evidence/i)).toBeNull();
    const loadedReport = screen.getByText('Persisted investigation loaded.')
      .closest('.ai');
    expect(loadedReport?.textContent).not.toMatch(/not assessed/i);
    expect(screen.getByText(/Incident details below summarize the saved alert evidence/)).toBeTruthy();
    expect(screen.queryByText(/LOCAL SIMULATION|simulated/i)).toBeNull();
    expect(screen.getByText('Persisted investigation loaded.')
      .closest('.ai')?.textContent).not.toContain('Not reported');
  });

  it('loads the selected incident from the backend even when the store is empty (refresh / direct link)', async () => {
    setStore({ incidents: { status: 'ready', data: [], error: null } });
    renderConsole(`/console?incident=${incident.incident_id}`);

    expect(await screen.findByRole('heading', { name: 'Incident investigation' }))
      .toBeTruthy();
    expect((await screen.findAllByText('CpuSaturation')).length).toBeGreaterThan(0);
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).endsWith(`/incidents/${incident.incident_id}`))).toBe(true);
  });

  it('reports an unknown incident ID as not found without opening another incident', async () => {
    fetchMock.mockImplementation(async () => response({ detail: 'Incident not found' }, 404));
    renderConsole('/console?incident=missing-demo');

    expect(await screen.findByRole('heading', { name: 'Incident not found' })).toBeTruthy();
    expect(screen.getByText('missing-demo')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Back to Console' })).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/workflow/'))).toBe(false);
  });

  it('shows a retryable error when the incident request fails', async () => {
    fetchMock.mockImplementation(async () => response({}, 500));
    renderConsole(`/console?incident=${incident.incident_id}`);

    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('shows the linked incident resolution in Investigation History', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/investigations')) {
        return response({
          investigations: [{
            investigation_id: 'investigation-cleared-123',
            operator_id: 'operator-a',
            incident_id: incident.incident_id,
            incident_status: 'RESOLVED',
            resolution_reason: 'condition_cleared',
            title: 'Cleared condition audit',
            message_count: 2,
            created_at: '2026-10-27T08:00:00+00:00',
            updated_at: '2026-10-27T08:02:00+00:00',
          }],
        });
      }
      return response({});
    });

    renderInvestigationHistory();

    expect(await screen.findByText('RESOLVED')).toBeTruthy();
    expect(screen.getByText('condition cleared')).toBeTruthy();
  });

  it('does not create records or run diagnosis/remediation when opening an incident', async () => {
    renderConsole(`/console?incident=${incident.incident_id}`);
    await screen.findByRole('heading', { name: 'Incident investigation' });

    const calls = fetchMock.mock.calls.map(([url, init]) => [String(url), (init as RequestInit | undefined)?.method || 'GET']);
    expect(calls.every(([, method]) => method === 'GET')).toBe(true);
    expect(calls.some(([url]) => url.includes('/workflow/') || url.includes('/remediat') || url.includes('/diagnose/'))).toBe(false);
  });

  it('shows the investigation landing state with a populated service selector', async () => {
    renderConsole('/console');

    expect(await screen.findByRole('heading', { name: 'Kybernaut Agent' })).toBeTruthy();
    const select = screen.getByLabelText('Service') as HTMLSelectElement;
    expect(select.options.length).toBeGreaterThan(0);
    expect(select.value).not.toBe('');
    expect(screen.getByRole('button', { name: /What's happening with the/ })).toBeTruthy();
    expect(screen.getByRole('tab', { name: 'Evidence' })).toBeTruthy();
    expect(screen.queryByText(/Investigation history is not available/)).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/workflow/'))).toBe(false);
  });

  it('runs the advisory workflow only after an explicit operator action', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/workflow/order-service')) {
        return response({
          service: 'order-service',
          policy: { action: 'investigate_monitoring', requires_human_approval: false },
          rca_status: 'unavailable',
        });
      }
      return response({ incidents: [] });
    });
    renderConsole('/console');
    await screen.findByRole('heading', { name: 'Kybernaut Agent' });
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/workflow/'))).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: /What's happening with the/ }));
    expect(await screen.findByText(/Advisory result · order-service/)).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes('/workflow/')).length).toBe(1);
  });

  it('routes a supported Kubernetes natural-language request through the scoped observer', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/kubernetes/incidents')) {
        return response({
          namespace: 'kybernaut-lab',
          incidents: [{
            type: 'pod_container_failure',
            severity: 'high',
            resource: 'demo-web-123',
            source_timestamp: '2026-10-27T08:01:00+00:00',
            evidence: ['Container entered CrashLoopBackOff'],
            recommendation: 'Inspect the recent container termination reason.',
          }],
          persistence: { persisted_count: 1 },
        });
      }
      return response({ incidents: [] });
    });
    renderConsole('/console');
    await screen.findByRole('heading', { name: 'Kybernaut Agent' });

    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Investigate current alerts in namespace kybernaut-lab' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText(/Kubernetes observation · Kubernetes · kybernaut-lab/)).toBeTruthy();
    expect(screen.getByText(/1 current Kubernetes incident\(s\) observed in kybernaut-lab/)).toBeTruthy();
    expect(screen.getByText(/Observer recommendation \(not RCA\)/)).toBeTruthy();
    expect(screen.getByText(/does not evaluate RCA or policy/)).toBeTruthy();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/kubernetes/incidents')).length).toBe(1);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/workflow/') || String(url).includes('/remediate'))).toBe(false);
  });

  it('loads persisted Kubernetes records and starts the bound RCA/policy workflow explicitly', async () => {
    const persisted = {
      incident_id: 'inc-k8s-console-1',
      incident_type: 'pod_container_failure',
      service: 'kubernetes-lab',
      severity: 'high',
      status: 'OPEN',
      fingerprint: 'k8s:kybernaut-lab:Pod:demo-web-123:pod_container_failure',
      source_evidence: {
        source: 'kubernetes',
        resource: 'demo-web-123',
        source_timestamp: '2026-10-27T08:01:00+00:00',
      },
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/kubernetes/incidents')) {
        return response({
          namespace: 'kybernaut-lab',
          incidents: [{
            type: 'pod_container_failure',
            severity: 'high',
            resource: 'demo-web-123',
            source_timestamp: '2026-10-27T08:01:00+00:00',
            evidence: ['Container entered CrashLoopBackOff'],
          }],
          persistence: { persisted_count: 1 },
        });
      }
      if (url.endsWith('/incidents/history?source=operational')) {
        return response({ incidents: [persisted] });
      }
      if (url.endsWith(`/incidents/${persisted.incident_id}/investigate`)
          && init?.method === 'POST') {
        return response({
          incident_id: persisted.incident_id,
          service: 'kubernetes-lab',
          status: 'awaiting_approval',
          rca_status: 'completed',
          rca: { root_cause: 'Possible startup configuration issue.' },
          classification: {
            incident_type: 'pod_crash',
            confidence: 0.85,
            evidence: ['Container entered CrashLoopBackOff'],
          },
          policy_decision: {
            allowed: true,
            requires_approval: true,
            auto_execute: false,
          },
          proposal: { action: 'restart_pod', resource: 'demo-web-123' },
          approval_id: 'apr-console-k8s-1',
        });
      }
      return response({ incidents: [] });
    });
    renderConsole('/console');
    await screen.findByRole('heading', { name: 'Kybernaut Agent' });

    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Investigate current pods in namespace kybernaut-lab' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    const investigate = await screen.findByRole('button', { name: 'Run RCA + policy' });
    fireEvent.click(investigate);

    expect(await screen.findByText(/Incident response workflow/)).toBeTruthy();
    expect(screen.getByText('awaiting_approval')).toBeTruthy();
    expect(screen.getAllByText(/Possible startup configuration issue/).length)
      .toBeGreaterThan(1);
    expect(screen.getByText('Not executed.')).toBeTruthy();
    const request = fetchMock.mock.calls.find(([url, init]) =>
      String(url).endsWith(`/incidents/${persisted.incident_id}/investigate`)
      && (init as RequestInit | undefined)?.method === 'POST');
    expect(request).toBeTruthy();
    expect(new Headers((request?.[1] as RequestInit).headers).get('Idempotency-Key'))
      .toBe(`console-k8s-${persisted.incident_id}-primary`);
  });

  it('rejects namespaces outside the verified Kubernetes observation scope', async () => {
    renderConsole('/console');
    await screen.findByRole('heading', { name: 'Kybernaut Agent' });

    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Show firing pod alerts in namespace production' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect((await screen.findByRole('alert')).textContent).toContain(
      'Read-only Kubernetes investigations are limited to kybernaut-lab',
    );
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/kubernetes/incidents'))).toBe(false);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/workflow/'))).toBe(false);
  });

  it('routes unsupported log searches to the grounded assistant, never to investigation endpoints', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/console/ask')) {
        return response({ answer: 'No logs are stored.', citations: [], llm_used: true, insufficient_data: true, context_keys: [], note: '' });
      }
      return response({});
    });
    renderConsole('/console');
    await screen.findByRole('heading', { name: 'Kybernaut Agent' });

    fireEvent.change(screen.getByRole('textbox', { name: 'Ask Kybernaut' }), {
      target: { value: 'Search application logs for connection timeouts' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));

    expect(await screen.findByText('No logs are stored.')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/workflow/') || String(url).endsWith('/kubernetes/incidents'))).toBe(false);
  });

  it('names the right-hand tab Remediation instead of Recs', async () => {
    renderConsole(`/console?incident=${incident.incident_id}`);
    await screen.findByRole('heading', { name: 'Incident investigation' });

    expect(screen.getByRole('tab', { name: /^Remediation/ })).toBeTruthy();
    expect(screen.queryByRole('tab', { name: /^Recs/ })).toBeNull();
  });

  it('shows Start investigation for an active incident and does not run it on open', async () => {
    renderConsole(`/console?incident=${incident.incident_id}`);

    expect(await screen.findByRole('button', { name: 'Start investigation' })).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/workflow/') || String(url).includes('/investigate'))).toBe(false);
  });

  it('shows application service scope and loads manual remediation guidance from the Remediation tab', async () => {
    const appIncident = {
      ...incident,
      service: 'payment-service',
      incident_type: 'memory_leak',
      source_evidence: { scenario: 'memory_leak' },
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/remediation-options')) {
        return response({
          incident_id: appIncident.incident_id,
          service: appIncident.service,
          incident_alert: 'Prometheus detected sustained memory growth.',
          source: 'telemetry',
          incident_type: 'memory_leak',
          live_evidence: false,
          note: 'Incident-specific operator remediation steps are listed below. This telemetry alert does not have a verified execution target and current evidence for Kybernaut to run an action safely.',
          options: [{
            option_id: 'manual-memory-diagnosis',
            kind: 'manual',
            executable: false,
            title: 'Confirm sustained memory growth in payment-service',
            steps: ['Compare fresh memory samples with the payment-service baseline.'],
            risk: 'Read-only.',
          }, {
            option_id: 'manual-memory-fix',
            kind: 'manual',
            executable: false,
            title: 'Fix the memory leak in payment-service',
            steps: ['Correct the confirmed cause in the owning code.'],
            risk: 'Reviewed change.',
          }, {
            option_id: 'manual-memory-verify',
            kind: 'manual',
            executable: false,
            title: 'Verify memory recovery in payment-service',
            steps: ['Run a representative payment-service business request successfully.'],
            risk: 'Read-only verification.',
          }],
        });
      }
      if (url.endsWith(`/incidents/${appIncident.incident_id}`)) return response(appIncident);
      if (url.endsWith('/timeline')) return response({ incident_id: appIncident.incident_id, events: [] });
      return response({});
    });
    renderConsole(`/console?incident=${appIncident.incident_id}`);

    expect((await screen.findAllByText('Application telemetry')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('Affected service').length).toBeGreaterThan(0);
    expect(screen.getAllByText('payment-service').length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole('tab', { name: /^Remediation/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Check remediation options' }));

    expect(await screen.findByText('No executable remediation is available yet')).toBeTruthy();
    expect(screen.getByText('Fix the memory leak in payment-service')).toBeTruthy();
    expect(screen.getByText(/representative payment-service business request/)).toBeTruthy();
    expect(screen.getAllByText(/verified execution target and current evidence/).length)
      .toBeGreaterThan(0);
    expect(screen.queryByText(/No action is available to run for this incident/)).toBeNull();
    expect(document.body.textContent).not.toMatch(/no Kubernetes target|workload target|simulat/i);
    expect(screen.queryByRole('button', { name: 'Execute' })).toBeNull();
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/remediation-options'))).toBe(true);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/execute-local-simulation'))).toBe(false);
  });

  it('never mentions simulated demo scenarios in the investigation view', async () => {
    renderConsole(`/console?incident=${incident.incident_id}`);
    await screen.findByRole('heading', { name: 'Incident investigation' });

    expect(screen.queryByText(/SIMULATED DEMO/i)).toBeNull();
  });

  const optionsPayload = {
    incident_id: incident.incident_id, service: 'user-service',
    incident_alert: 'Prometheus anomaly: cpu spike observed.',
    source: 'telemetry', incident_type: 'cpu_spike', live_evidence: false,
    note: 'Incident-specific operator remediation steps are listed below. This telemetry alert does not have a verified execution target and current evidence for Kybernaut to run an action safely.',
    options: [{ option_id: 'action-scale', kind: 'executable', executable: false,
      action: 'scale_deployment', title: 'Scale the deployment out by one replica (user-service)',
      runbook_reason: 'incident_not_current' }, ...[1, 2, 3].map(n => ({ option_id: `manual-${n}`, kind: 'manual', executable: false,
      title: `Guidance ${n}`, steps: ['Check the dashboard'], risk: 'Read-only.' })),
    ],
  };

  it('Start investigation shows a step list and a set of remediation options without running anything', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/remediation-options')) return response(optionsPayload);
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith('/timeline')) return response({ incident_id: incident.incident_id, events: [] });
      return response({});
    });
    renderConsole(`/console?incident=${incident.incident_id}`);

    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));

    expect(await screen.findByText('Investigation complete')).toBeTruthy();
    // Nothing is runnable here, so the chat says so instead of listing every option.
    expect(screen.getByText('No safe automatic action is available for this alert')).toBeTruthy();
    expect(document.body.textContent).toContain('Incident alert: Prometheus anomaly: cpu spike observed.');
    expect(document.body.textContent).toContain('Affected service: user-service');
    expect(screen.getByText(/3 recovery guidance steps/)).toBeTruthy();
    expect(screen.getAllByRole('link', { name: 'Incidents & Alerts' })
      .some(link => link.getAttribute('href') === '/incidents')).toBe(true);
    const remediationTab = screen.getByRole('tab', { name: 'Remediation (4)' });
    await waitFor(() => expect(remediationTab.getAttribute('aria-selected')).toBe('true'));
    expect(await screen.findByText('Available options')).toBeTruthy();
    expect(screen.getByText('Guidance 1')).toBeTruthy();
    expect(screen.getAllByText('Check the dashboard').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: 'Execute' })).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: 'Investigation' }));
    expect(screen.getAllByText('OPEN').length).toBeGreaterThan(0);
    expect(screen.getByText('Unavailable — current evidence not confirmed')).toBeTruthy();
    expect(screen.getAllByText(/Incident-specific operator remediation steps are listed below/).length)
      .toBeGreaterThan(0);
    const calls = fetchMock.mock.calls.map(([url, init]) => [String(url), (init as RequestInit | undefined)?.method || 'GET']);
    expect(calls.every(([, method]) => method === 'GET')).toBe(true);
    expect(calls.some(([url]) => url.includes('/investigate') || url.includes('/workflow/'))).toBe(false);
  });

  it('executes the incident-specific recovery action and reports resolution', async () => {
    const simulatedIncident = {
      ...incident,
      service: 'payment-service',
      incident_type: 'http_5xx',
      source_evidence: {
        simulation: 'local_service_failure',
        simulated: true,
        service: 'payment-service',
        scenario: 'http_5xx',
      },
    };
    setStore({ permissions: ['read', 'remediation'] });
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/remediation-options')) {
        return response({
          incident_id: incident.incident_id,
          incident_status: 'OPEN',
          source: 'local_service_simulator',
          incident_type: 'http_5xx',
          note: 'Local simulation for payment-service; these service-specific suggestions are manual only. No remediation was executed.',
          options: [{
            option_id: 'clear-local-simulation',
            kind: 'executable',
            executable: true,
            action: 'clear_local_simulation',
            resource: 'payment-service',
            title: 'Clear the simulated fault on payment-service',
            steps: [
              "Clear only this service's active simulator fault.",
              'Resolve this incident after the service confirms the fault is inactive.',
            ],
            risk: 'Restores the selected local service; does not execute changes in Kubernetes.',
          }],
        });
      }
      if (url.endsWith('/execute-local-simulation')) {
        expect(init?.method).toBe('POST');
        return response({
          incident_id: incident.incident_id,
          service: 'payment-service',
          resolved: true,
          audit_recorded: true,
        });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) {
        return response(simulatedIncident);
      }
      if (url.includes('/incidents/history')) return response({ incidents: [] });
      if (url.endsWith('/timeline')) {
        return response({ incident_id: incident.incident_id, events: [] });
      }
      return response({});
    });

    renderConsole(`/console?incident=${incident.incident_id}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));
    const localClassification = await screen.findByText(
      'Classifying the incident from available evidence',
    );
    expect(localClassification.closest('.step-done')).not.toBeNull();
    expect(screen.getByText(/Classified as http 5xx for payment-service from incident evidence/)).toBeTruthy();
    const recoveryTitles = screen.getAllByText(
      'Stop the HTTP 503 error responses on payment-service',
    );
    expect(recoveryTitles.length).toBeGreaterThan(0);
    const recoveryCard = recoveryTitles[0].closest('.option');
    expect(recoveryCard?.textContent).not.toMatch(/simulat|Kubernetes/i);
    expect(recoveryCard?.textContent).toContain('payment authorisation requests');
    expect(recoveryCard?.textContent).toContain('success status');
    expect(document.body.textContent).not.toContain('Local simulation for payment-service');
    // The question and the Execute button appear in the conversation itself.
    expect(await screen.findByText('Shall I run this now? It needs your approval.')).toBeTruthy();
    fireEvent.click(await screen.findByRole('button', { name: 'Execute' }));

    // Step-by-step progress ends in a short report box.
    expect(await screen.findByText(
      'Stop the HTTP 503 error responses on payment-service completed, and the incident is now resolved.',
    )).toBeTruthy();
    const report = screen.getByLabelText('Execution report');
    expect(report).toBeTruthy();
    expect(report.textContent).not.toMatch(/simulat|local simulator/i);
    expect(report.textContent).not.toContain('Scope');
    expect(report.textContent).not.toContain('Ask "show the audit"');
    const progressHeading = screen.getByText('Incident resolution progress');
    const progress = progressHeading.closest('.stepsbubble');
    expect(progress).not.toBeNull();
    expect(progress!.textContent).not.toMatch(/simulat/i);
    expect(progress!.querySelectorAll('.step-done')).toHaveLength(5);
    for (const stage of ['Investigated', 'Approval', 'Executed', 'Verified', 'Resolved']) {
      expect(progress!.textContent).toContain(stage);
    }
    expect(screen.getByText('Condition inactive · incident RESOLVED')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url, init]) =>
      String(url).endsWith('/execute-local-simulation') &&
      (init as RequestInit | undefined)?.method === 'POST')).toBe(true);
  });

  it('surfaces an unreachable service and keeps the failed step explicit', async () => {
    const simulatedIncident = {
      ...incident,
      service: 'inventory-service',
      incident_type: 'request_failure',
      source_evidence: {
        simulation: 'local_service_failure',
        simulated: true,
        service: 'inventory-service',
        scenario: 'request_failure',
      },
    };
    setStore({ permissions: ['read', 'remediation'] });
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/remediation-options')) {
        return response({
          incident_id: incident.incident_id,
          incident_status: 'OPEN',
          source: 'local_service_simulator',
          incident_type: 'request_failure',
          options: [{
            option_id: 'clear-local-simulation',
            kind: 'executable',
            executable: true,
            action: 'clear_local_simulation',
            resource: 'inventory-service',
            title: 'Clear the simulated fault on inventory-service',
          }],
        });
      }
      if (url.endsWith('/execute-local-simulation')) {
        expect(init?.method).toBe('POST');
        return response({
          detail: 'Could not reach inventory-service. Confirm the service is running; the incident remains open.',
        }, 502);
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) {
        return response(simulatedIncident);
      }
      if (url.includes('/incidents/history')) return response({ incidents: [] });
      if (url.endsWith('/timeline')) {
        return response({ incident_id: incident.incident_id, events: [] });
      }
      return response({});
    });

    renderConsole(`/console?incident=${incident.incident_id}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Execute' }));

    expect((await screen.findAllByText(
      /Could not reach inventory-service/,
    )).length).toBeGreaterThan(0);
    const failureHeading = screen.getByText('Execution failed');
    const progress = failureHeading.closest('.stepsbubble');
    expect(progress?.querySelectorAll('.step-done')).toHaveLength(2);
    expect(progress?.textContent).toContain('The backend did not confirm successful resolution.');
    expect(screen.getAllByText('OPEN').length).toBeGreaterThan(0);
  });

  it('explains blocked runbook prerequisites next to the open incident status', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/remediation-options')) {
        return response({
          incident_id: incident.incident_id,
          incident_type: 'pod_crash',
          live_evidence: true,
          options: [{
            kind: 'executable',
            executable: false,
            action: 'restart_pod',
            runbook_reason: 'minimum_ready_replicas_not_met',
            title: 'Recreate the affected pod',
          }],
        });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith('/timeline')) return response({ incident_id: incident.incident_id, events: [] });
      return response({});
    });
    renderConsole(`/console?incident=${incident.incident_id}`);

    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));
    await screen.findByText('Investigation complete');
    fireEvent.click(screen.getByRole('tab', { name: 'Investigation' }));

    expect(screen.getAllByText('OPEN').length).toBeGreaterThan(0);
    expect(screen.getByText('Unavailable — runbook prerequisite not met')).toBeTruthy();
    expect(screen.getByText(/requires more ready replicas/i)).toBeTruthy();
  });

  it('explains when the action budget blocks otherwise eligible options', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/remediation-options')) {
        return response({
          incident_id: incident.incident_id,
          incident_type: 'memory_leak',
          live_evidence: true,
          options: [{
            kind: 'executable',
            executable: true,
            policy_available: false,
            policy_reason: 'action_claim_limit_reached',
            policy_detail: 'Action limit reached (5/5) for the last 3600 seconds.',
            action: 'restore_command',
            title: 'Stop the simulator memory writer',
          }],
        });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith('/timeline')) return response({ incident_id: incident.incident_id, events: [] });
      return response({});
    });
    renderConsole(`/console?incident=${incident.incident_id}`);

    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));
    await screen.findByText('Investigation complete');
    fireEvent.click(screen.getByRole('tab', { name: 'Investigation' }));

    expect(screen.getByText('Unavailable — action limit reached')).toBeTruthy();
    expect(screen.getAllByText(/Action limit reached \(5\/5\)/).length).toBeGreaterThanOrEqual(2);
  });

  it('one Execute click approves the generated plan and reports resolution only after verification', async () => {
    setStore({ permissions: ['read', 'remediation'] });
    const options = {
      incident_id: incident.incident_id,
      source: 'kubernetes',
      incident_type: 'deployment_failure',
      live_evidence: true,
      options: [{
        option_id: 'action-scale_deployment',
        kind: 'executable',
        executable: true,
        action: 'scale_deployment',
        resource: 'web',
        title: 'Scale the deployment out by one replica (web)',
      }],
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/remediation-options')) return response(options);
      if (url.endsWith('/agent/status')) return response({ remediation_mode: 'dry_run' });
      if (url.includes('/investigate?preferred_action=scale_deployment')) {
        expect(new Headers(init?.headers).get('Idempotency-Key'))
          .toMatch(/^console-execute-/);
        return response({
          status: 'awaiting_approval',
          approval_id: 'apr-console-execute',
          proposal: { action: 'scale_deployment', resource: 'web' },
          policy_decision: { allowed: true, requires_approval: true },
        });
      }
      if (url.endsWith('/approvals/apr-console-execute/decision')) {
        expect(init?.method).toBe('POST');
        expect(JSON.parse(String(init?.body))).toEqual({
          decision: 'approved',
          reason: 'Approved from the Console Execute action.',
        });
        return response({
          status: 'resolved',
          execution: { mode: 'live', executed: true, ok: true },
          verification: { recovered: true, reason: 'workload healthy' },
        });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith(`/incidents/${incident.incident_id}/timeline`)) {
        return response({ incident_id: incident.incident_id, events: [] });
      }
      if (url.includes('/approvals?')) return response({ approvals: [] });
      return response({});
    });
    renderConsole(`/console?incident=${incident.incident_id}`);

    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Execute' }));

    expect(await screen.findByText('Incident resolved')).toBeTruthy();
    expect(screen.getByLabelText('Execution report').textContent)
      .not.toContain('Ask "show the audit"');
    fireEvent.click(screen.getByRole('tab', { name: /Remediation/ }));
    expect(await screen.findByText('Latest remediation outcome')).toBeTruthy();
    expect(await screen.findByText('Resolved: recovery was verified.')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).includes('/investigate?preferred_action=scale_deployment'))).toBe(true);
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).endsWith('/approvals/apr-console-execute/decision'))).toBe(true);
  });

  it('shows that dry-run did not execute and keeps the incident unresolved in the sidebar', async () => {
    setStore({ permissions: ['read', 'remediation'] });
    const options = {
      incident_id: incident.incident_id,
      source: 'kubernetes',
      incident_type: 'deployment_failure',
      live_evidence: true,
      options: [{
        option_id: 'action-scale_deployment',
        kind: 'executable',
        executable: true,
        action: 'scale_deployment',
        resource: 'web',
        title: 'Scale the deployment out by one replica (web)',
      }],
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/remediation-options')) return response(options);
      if (url.includes('/investigate?preferred_action=scale_deployment')) {
        return response({
          status: 'awaiting_approval',
          approval_id: 'apr-console-dry-run',
          proposal: { action: 'scale_deployment', resource: 'web' },
          policy_decision: { allowed: true, requires_approval: true },
        });
      }
      if (url.endsWith('/approvals/apr-console-dry-run/decision')) {
        expect(init?.method).toBe('POST');
        return response({
          status: 'dry_run_complete',
          execution: {
            mode: 'dry_run',
            executed: false,
            ok: true,
            command: ['kubectl', 'scale', 'deployment/web'],
          },
          verification: { recovered: null, reason: 'not_executed' },
        });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith(`/incidents/${incident.incident_id}/timeline`)) {
        return response({ incident_id: incident.incident_id, events: [] });
      }
      if (url.includes('/approvals?')) return response({ approvals: [] });
      return response({});
    });
    renderConsole(`/console?incident=${incident.incident_id}`);

    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));
    expect(await screen.findByText('I found 1 action I can run')).toBeTruthy();
    const execute = await screen.findByRole('button', { name: 'Execute' });
    await waitFor(() => expect((execute as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole('button', { name: 'Execute' }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) =>
      String(url).includes('/approvals?limit=100'))).toBe(true));
    expect(await screen.findAllByText('Dry run complete')).toHaveLength(2);

    fireEvent.click(screen.getByRole('tab', { name: /Remediation/ }));
    expect(await screen.findByText('Latest remediation outcome')).toBeTruthy();
    expect(await screen.findByText('DRY RUN · NOT EXECUTED')).toBeTruthy();
    expect(screen.getByText(/approved command was recorded but not executed/i)).toBeTruthy();
    expect(screen.queryByText('Resolved: recovery was verified.')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: 'Investigation' }));
    expect(screen.getByText('Candidate available · dry-run only')).toBeTruthy();
    expect(screen.getByText('DRY RUN · WILL NOT EXECUTE')).toBeTruthy();
    expect(screen.getByText(/Approval will not change the cluster/i)).toBeTruthy();
  });

  it('surfaces a policy block and does not submit approval when a safety check fails', async () => {
    setStore({ permissions: ['read', 'remediation'] });
    const options = {
      incident_id: incident.incident_id,
      source: 'kubernetes',
      incident_type: 'deployment_failure',
      live_evidence: true,
      options: [{
        option_id: 'action-scale_deployment',
        kind: 'executable',
        executable: true,
        action: 'scale_deployment',
        resource: 'web',
        title: 'Scale the deployment out by one replica (web)',
      }],
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/remediation-options')) return response(options);
      if (url.includes('/investigate?preferred_action=scale_deployment')) {
        return response({
          status: 'blocked',
          proposal: { action: 'scale_deployment', resource: 'web' },
          approval_id: null,
          policy_decision: {
            allowed: false,
            requires_approval: false,
            reasons: ['rate_limit: recent_actions=3 limit=3 per 3600s'],
            rules: [{
              id: 'rate_limit',
              passed: false,
              detail: 'recent_actions=3 limit=3 per 3600s',
            }],
          },
        });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith(`/incidents/${incident.incident_id}/timeline`)) {
        return response({ incident_id: incident.incident_id, events: [] });
      }
      if (url.includes('/approvals?')) return response({ approvals: [] });
      return response({});
    });
    renderConsole(`/console?incident=${incident.incident_id}`);

    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Execute' }));

    expect(await screen.findByText(/Policy blocked this action: rate_limit/)).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).includes('/approvals/') && String(url).endsWith('/decision'))).toBe(false);
    const investigation = fetchMock.mock.calls.find(([url, init]) =>
      String(url).includes('/investigate?preferred_action=scale_deployment')
      && (init as RequestInit | undefined)?.method === 'POST');
    expect(new Headers((investigation?.[1] as RequestInit).headers).get('Idempotency-Key'))
      .toMatch(/^console-execute-/);
  });

  it('uses the existing pending approval for the selected action and shows verified recovery', async () => {
    setStore({ permissions: ['read', 'remediation'], operatorId: 'operator-a' });
    const options = {
      incident_id: incident.incident_id,
      source: 'kubernetes',
      incident_type: 'deployment_failure',
      live_evidence: true,
      options: [{
        option_id: 'action-scale_deployment',
        kind: 'executable',
        executable: true,
        action: 'scale_deployment',
        resource: 'web',
        target_replicas: 2,
        title: 'Scale the deployment out by one replica (web)',
      }],
    };
    const pendingApproval = {
      approval_id: 'apr-existing',
      incident_ref: incident.incident_id,
      status: 'PENDING',
      proposed_action: {
        action: 'scale_deployment',
        resource: 'web',
        target_replicas: 2,
      },
    };
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/remediation-options')) return response(options);
      if (url.includes('/approvals?')) return response({ approvals: [pendingApproval] });
      if (url.endsWith('/approvals/apr-existing/decision')) {
        expect(init?.method).toBe('POST');
        return response({
          status: 'resolved',
          execution: { mode: 'live', executed: true, ok: true },
          verification: { recovered: true, reason: 'healthy_observation' },
        });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith(`/incidents/${incident.incident_id}/timeline`)) {
        return response({ incident_id: incident.incident_id, events: [] });
      }
      return response({});
    });
    renderConsole(`/console?incident=${incident.incident_id}`);

    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Execute' }));

    expect(await screen.findByText('Using the existing pending approval for this incident and action.')).toBeTruthy();
    expect(await screen.findByText('Resolved: recovery was verified.')).toBeTruthy();
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).includes('/investigate?preferred_action='))).toBe(false);
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).endsWith('/approvals/apr-existing/decision'))).toBe(true);
  });

  it('shows a cleared condition as resolved without offering remediation', async () => {
    setStore({ permissions: ['read', 'remediation'] });
    const resolvedIncident = {
      ...incident,
      status: 'RESOLVED',
      resolution_reason: 'condition_cleared',
      resolved_at: '2026-10-27T08:02:00+00:00',
    };
    const options = {
      incident_id: incident.incident_id,
      incident_status: 'RESOLVED',
      condition_cleared: true,
      resolution_reason: 'condition_cleared',
      source: 'kubernetes',
      incident_type: 'pod_crash',
      live_evidence: false,
      note: 'Resolved — a complete fresh Kubernetes observation confirmed the condition had cleared before remediation. No remediation was executed.',
      options: [{
        option_id: 'manual-1',
        kind: 'manual',
        executable: false,
        title: 'Review the workload',
        steps: ['Inspect the deployment'],
        risk: 'Read-only.',
      }],
    };
    let conditionCleared = false;
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/remediation-options')) {
        conditionCleared = true;
        return response(options);
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) {
        return response(conditionCleared ? resolvedIncident : incident);
      }
      if (url.endsWith(`/incidents/${incident.incident_id}/timeline`)) {
        return response({
          incident_id: incident.incident_id,
          events: [{
            timestamp: '2026-10-27T08:02:00+00:00',
            event_type: 'incident_resolved',
            actor: 'kybernaut-monitor',
            outcome: 'condition_cleared',
            summary: 'Incident condition cleared before remediation.',
          }],
        });
      }
      return response({});
    });

    renderConsole(`/console?incident=${incident.incident_id}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));

    expect((await screen.findAllByText(/fresh observation confirmed the condition cleared; no remediation was executed/i)).length).toBeGreaterThan(0);
    expect(screen.getAllByText('RESOLVED').length).toBeGreaterThan(0);
    expect(await screen.findByText('Incident resolved · no Execute action')).toBeTruthy();
    expect(screen.getByText(/saved suggestions below are informational/i)).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Execute' })).toBeNull();
    expect(fetchMock.mock.calls.filter(([url]) =>
      String(url).endsWith(`/incidents/${incident.incident_id}`)).length).toBeGreaterThan(1);

    fireEvent.click(screen.getByRole('tab', { name: 'Audit' }));
    expect(await screen.findByText(/incident resolved/i)).toBeTruthy();
    expect(screen.getByText('condition_cleared')).toBeTruthy();
  });

  it('shows the runbook replica threshold and observed count for blocked actions', async () => {
    setStore({ permissions: ['read', 'remediation'] });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/remediation-options')) {
        return response({
          incident_id: incident.incident_id,
          incident_status: 'OPEN',
          source: 'kubernetes',
          incident_type: 'pod_crash',
          live_evidence: true,
          options: [{
            option_id: 'action-restart_pod',
            kind: 'executable',
            executable: false,
            action: 'restart_pod',
            resource: 'sim-pod-crash-1',
            title: 'Recreate the affected pod',
            runbook_reason: 'minimum_ready_replicas_not_met',
            runbook_prerequisites: { min_ready_replicas: 2 },
            observed_available_replicas: 1,
          }],
        });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith(`/incidents/${incident.incident_id}/timeline`)) {
        return response({ incident_id: incident.incident_id, events: [] });
      }
      return response({});
    });

    renderConsole(`/console?incident=${incident.incident_id}`);
    fireEvent.click(await screen.findByRole('button', { name: 'Start investigation' }));

    expect(await screen.findAllByText(
      'Runbook requires at least 2 ready replicas; observed 1 available replica.',
    )).toHaveLength(2);
    // Blocked actions are explained but never offered as an Execute button.
    expect(screen.queryByRole('button', { name: 'Execute' })).toBeNull();
  });

  it('answers a free-form question through the grounded assistant endpoint', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/console/ask')) {
        return response({ answer: 'Incident inc-test-123 is OPEN.', citations: ['incident'], llm_used: true, insufficient_data: false, context_keys: ['incident'], note: '' });
      }
      if (url.endsWith(`/incidents/${incident.incident_id}`)) return response(incident);
      if (url.endsWith('/timeline')) return response({ incident_id: incident.incident_id, events: [] });
      return response({});
    });
    renderConsole(`/console?incident=${incident.incident_id}`);
    await screen.findByRole('heading', { name: 'Incident investigation' });

    const box = screen.getByLabelText('Ask Kybernaut');
    fireEvent.change(box, { target: { value: 'Why did this happen?' } });
    fireEvent.keyDown(box, { key: 'Enter' });

    expect(await screen.findByText('Incident inc-test-123 is OPEN.')).toBeTruthy();
    const ask = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/console/ask'));
    expect(JSON.parse(String((ask?.[1] as RequestInit).body))).toMatchObject({ incident_id: incident.incident_id });
  });

  it('has no back button in the investigation bar', async () => {
    renderConsole(`/console?incident=${incident.incident_id}`);
    await screen.findByRole('heading', { name: 'Incident investigation' });

    expect(screen.queryByRole('button', { name: 'Back' })).toBeNull();
  });
});