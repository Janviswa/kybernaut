import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import App from './App';
import Approvals from './pages/Approvals';
import Login from './pages/Login';
import { apiRequest, hasToken, restoreOperatorSession } from './api';
import { set as setStore, signOut, useAuth } from './store';

const response = (body: unknown, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

function AuthenticationState() {
  const authenticated = useAuth();
  return <span>{authenticated ? 'session active' : 'session cleared'}</span>;
}

function renderLogin() {
  return render(
    <MemoryRouter initialEntries={['/login']}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/console" element={<AuthenticationState />} />
      </Routes>
    </MemoryRouter>,
  );
}

function renderApp(initialEntry = '/login') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <App />
    </MemoryRouter>,
  );
}

function renderApprovals(
  permissions: string[],
  status = 'PENDING',
  executionStatus?: string,
  requestedBy = 'requester',
) {
  signOut();
  setStore({
    authenticated: true,
    operatorId: requestedBy === 'test-operator' ? 'test-operator' : 'different-operator',
    permissions,
    approvals: {
      status: 'ready',
      data: [{
        approval_id: 'apr-test',
        status,
        ...(executionStatus ? { execution_status: executionStatus } : {}),
        requested_by: requestedBy,
        incident_ref: 'incident-test',
        proposed_action: {},
        context: {},
      }],
      error: null,
    },
  });
  return render(
    <MemoryRouter>
      <Approvals />
    </MemoryRouter>,
  );
}

describe('operator authentication UI', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    signOut();
    fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/auth/login')) {
        return response({
          access_token: 'short-lived-test-token',
          token_type: 'bearer',
          expires_in: 900,
          operator_id: 'read-only',
          permissions: ['read'],
        });
      }
      if (url.endsWith('/protected')) return response({ ok: true });
      if (url.includes('/incidents/history?source=operational')) {
        return response({ incidents: [], source_filter: 'operational' });
      }
      return response({});
    });
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    signOut();
    cleanup();
    vi.unstubAllGlobals();
  });

  it('persists the short-lived operator session for this tab and authenticates API requests', async () => {
    renderLogin();
    fireEvent.change(screen.getByLabelText('Username'), {
      target: { value: 'read-only' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'test-password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    await screen.findByText('session active');
    expect(hasToken()).toBe(true);
    expect(restoreOperatorSession()).toEqual(expect.objectContaining({
      operatorId: 'read-only',
      permissions: ['read'],
    }));
    expect(hasToken()).toBe(true);
    const [loginUrl, loginInit] = fetchMock.mock.calls[0];
    expect(String(loginUrl)).toBe('/api/auth/login');
    expect(loginInit?.method).toBe('POST');
    expect(JSON.parse(String(loginInit?.body))).toEqual({
      username: 'read-only',
      password: 'test-password',
    });

    expect(new Headers(loginInit?.headers).has('Authorization')).toBe(false);

    await expect(apiRequest('/protected')).resolves.toEqual({ ok: true });
    const protectedInit = fetchMock.mock.calls.find(([url]) =>
      String(url).endsWith('/protected'),
    )?.[1];
    expect(new Headers(protectedInit?.headers).get('Authorization'))
      .toBe('Bearer short-lived-test-token');
  });

  it('accepts and restores the trusted owner permission returned by login', async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/auth/login')) {
        return response({
          access_token: 'owner-access-token',
          token_type: 'bearer',
          expires_in: 900,
          operator_id: 'test-operator',
          permissions: ['owner', 'read', 'remediation'],
        });
      }
      return response({});
    });

    renderLogin();
    fireEvent.change(screen.getByLabelText('Username'), {
      target: { value: 'test-operator' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'test-password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    await screen.findByText('session active');
    expect(restoreOperatorSession()).toEqual(expect.objectContaining({
      operatorId: 'test-operator',
      permissions: ['owner', 'read', 'remediation'],
    }));
    expect(hasToken()).toBe(true);
  });

  it('restores a stored owner session with owner capabilities', () => {
    window.sessionStorage.setItem('kybernaut.operator-session', JSON.stringify({
      accessToken: 'owner-access-token',
      operatorId: 'test-operator',
      permissions: ['owner', 'read', 'remediation'],
      expiresAt: Date.now() + 60_000,
    }));

    expect(restoreOperatorSession()).toEqual(expect.objectContaining({
      operatorId: 'test-operator',
      permissions: ['owner', 'read', 'remediation'],
    }));
    expect(hasToken()).toBe(true);
  });

  it('discards an expired stored operator session', () => {
    window.sessionStorage.setItem('kybernaut.operator-session', JSON.stringify({
      accessToken: 'expired-test-token',
      operatorId: 'read-only',
      permissions: ['read'],
      expiresAt: Date.now() - 1,
    }));

    expect(restoreOperatorSession()).toBeNull();
    expect(hasToken()).toBe(false);
    expect(window.sessionStorage.getItem('kybernaut.operator-session')).toBeNull();
  });

  it('loads Overview data on the first mount after restoring an authenticated session', async () => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    signOut();
    window.sessionStorage.setItem('kybernaut.operator-session', JSON.stringify({
      accessToken: 'restored-session-test-token',
      operatorId: 'read-only',
      permissions: ['read'],
      expiresAt: Date.now() + 60_000,
    }));
    const restored = restoreOperatorSession();
    setStore({
      authenticated: true,
      operatorId: restored?.operatorId ?? null,
      permissions: restored?.permissions ?? [],
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/health')) {
        const checkedAt = '2026-01-01T00:00:00Z';
        const component = (status: string, reason: string) => ({
          status,
          checked_at: checkedAt,
          reason,
        });
        return response({
          status: 'healthy',
          service: 'api',
          version: 'test',
          checked_at: checkedAt,
          components: {
            api: component('healthy', 'Health endpoint responded.'),
            prometheus: component('degraded', 'Reachable; health response was incomplete.'),
            kubernetes_lab: component('unknown', 'Lab access check timed out.'),
            database: component('unhealthy', 'Database connectivity query failed.'),
          },
        });
      }
      if (url.includes('/incidents/history?source=operational')) {
        return response({ incidents: [] });
      }
      if (url.includes('/diagnose/')) {
        const service = decodeURIComponent(url.split('/diagnose/')[1]);
        return response({ service, status: 'up', findings: [] });
      }
      if (url.includes('/approvals?')) return response({ approvals: [] });
      if (url.includes('/audit?')) return response({ events: [] });
      if (url.endsWith('/metrics/incidents')) {
        return response({
          total_incidents: 0,
          active_incidents: 0,
          by_status: {},
          by_severity: {},
          resolved_incidents_with_duration: 0,
          mttr_seconds: null,
          recurrence_rate: 0,
        });
      }
      if (url.endsWith('/cost/report')) {
        return response({ summary: {}, recommendations: [] });
      }
      return response({});
    });

    renderApp('/overview');

    expect(await screen.findByText('No persisted incidents.')).toBeTruthy();
    expect(await screen.findByText('No audit events returned.')).toBeTruthy();
    const protectedRequests = fetchMock.mock.calls.filter(([url]) =>
      !String(url).endsWith('/auth/login') &&
      !String(url).endsWith('/health'),
    );
    expect(protectedRequests.length).toBeGreaterThan(5);
    expect(protectedRequests.every(([, init]) =>
      new Headers((init as RequestInit).headers).has('Authorization'),
    )).toBe(true);
    expect(screen.queryByText('Loading backend data…')).toBeNull();
  });

  it('uses global lifecycle counts for sidebar badges rather than loaded page samples', async () => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    signOut();
    window.sessionStorage.setItem('kybernaut.operator-session', JSON.stringify({
      accessToken: 'restored-session-test-token',
      operatorId: 'read-only',
      permissions: ['read'],
      expiresAt: Date.now() + 60_000,
    }));
    const restored = restoreOperatorSession();
    setStore({
      authenticated: true,
      operatorId: restored?.operatorId ?? null,
      permissions: restored?.permissions ?? [],
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/metrics/incidents')) {
        return response({
          active_incidents: 124,
          pending_approvals: 101,
        });
      }
      if (url.includes('/incidents/history?source=operational')) {
        return response({ incidents: [] });
      }
      if (url.includes('/approvals?')) return response({ approvals: [] });
      return response({});
    });

    renderApp('/overview');

    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'Incidents & Alerts' })
        .textContent).toContain('124');
      expect(screen.queryByRole('link', { name: 'Approvals & Remediation' })).toBeNull();
    });
  });

  it('gets pending approval counts from the authorized approvals route when metrics are older', async () => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    signOut();
    window.sessionStorage.setItem('kybernaut.operator-session', JSON.stringify({
      accessToken: 'restored-session-test-token',
      operatorId: 'read-only',
      permissions: ['read'],
      expiresAt: Date.now() + 60_000,
    }));
    const restored = restoreOperatorSession();
    setStore({
      authenticated: true,
      operatorId: restored?.operatorId ?? null,
      permissions: restored?.permissions ?? [],
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/metrics/incidents')) {
        return response({ active_incidents: 6 });
      }
      if (url.endsWith('/approvals?status=PENDING&limit=1000')) {
        return response({
          approvals: [
            { approval_id: 'apr-one', status: 'PENDING' },
            { approval_id: 'apr-two', status: 'PENDING' },
            { approval_id: 'apr-done', status: 'APPROVED' },
          ],
        });
      }
      if (url.includes('/incidents/history?source=operational')) {
        return response({ incidents: [] });
      }
      if (url.includes('/approvals?')) return response({ approvals: [] });
      return response({});
    });

    renderApp('/overview');

    await waitFor(() => {
      expect(screen.getByRole('link', { name: 'Incidents & Alerts' })
        .textContent).toContain('6');
      expect(screen.queryByRole('link', { name: 'Approvals & Remediation' })).toBeNull();
    });
    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).endsWith('/approvals?status=PENDING&limit=1000'),
    )).toBe(true);
  });

  it('does not show zero badges when the lifecycle-count route returns 404', async () => {
    vi.stubGlobal('ResizeObserver', class {
      observe() {}
      unobserve() {}
      disconnect() {}
    });
    signOut();
    window.sessionStorage.setItem('kybernaut.operator-session', JSON.stringify({
      accessToken: 'restored-session-test-token',
      operatorId: 'read-only',
      permissions: ['read'],
      expiresAt: Date.now() + 60_000,
    }));
    const restored = restoreOperatorSession();
    setStore({
      authenticated: true,
      operatorId: restored?.operatorId ?? null,
      permissions: restored?.permissions ?? [],
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/metrics/incidents')) {
        return response({ detail: 'not found' }, 404);
      }
      if (String(input).includes('/incidents/history?source=operational')) {
        return response({ incidents: [] });
      }
      if (String(input).includes('/approvals?')) {
        return response({ approvals: [] });
      }
      return response({});
    });

    renderApp('/overview');

    await waitFor(() => {
      expect(fetchMock.mock.calls.some(([url]) =>
        String(url).endsWith('/metrics/incidents'),
      )).toBe(true);
    });
    expect(await screen.findByText(
      /Sidebar counts unavailable:.*HTTP 404/,
    )).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Incidents & Alerts' })
      .querySelector('.cnt')).toBeNull();
    expect(screen.queryByRole('link', { name: 'Approvals & Remediation' })).toBeNull();
  });

  it('renders independent, evidence-backed system dependency states', async () => {
    signOut();
    window.sessionStorage.setItem('kybernaut.operator-session', JSON.stringify({
      accessToken: 'restored-session-test-token',
      operatorId: 'read-only',
      permissions: ['read'],
      expiresAt: Date.now() + 60_000,
    }));
    const restored = restoreOperatorSession();
    setStore({
      authenticated: true,
      operatorId: restored?.operatorId ?? null,
      permissions: restored?.permissions ?? [],
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/health')) {
        const checkedAt = '2026-01-01T00:00:00Z';
        const component = (status: string, reason: string) => ({
          status,
          checked_at: checkedAt,
          reason,
        });
        return response({
          status: 'healthy',
          service: 'api',
          version: 'test',
          checked_at: checkedAt,
          components: {
            api: component('healthy', 'Health endpoint responded.'),
            prometheus: component('degraded', 'Reachable; health response was incomplete.'),
            kubernetes_lab: component('unknown', 'Lab access check timed out.'),
            database: component('unhealthy', 'Database connectivity query failed.'),
          },
        });
      }
      return response({});
    });

    renderApp('/overview');

    expect(await screen.findByLabelText(
      'API (FastAPI) status: Healthy. Health endpoint responded. Checked at 2026-01-01T00:00:00Z.',
    )).toBeTruthy();
    expect(screen.getByLabelText(
      'Prometheus status: Degraded. Reachable; health response was incomplete. Checked at 2026-01-01T00:00:00Z.',
    )).toBeTruthy();
    expect(screen.getByLabelText(
      'Kubernetes (Lab) status: Unknown. Lab access check timed out. Checked at 2026-01-01T00:00:00Z.',
    )).toBeTruthy();
    expect(screen.getByLabelText(
      'Database status: Unhealthy. Database connectivity query failed. Checked at 2026-01-01T00:00:00Z.',
    )).toBeTruthy();
  });

  it('shows a generic credential error and does not authenticate on 401', async () => {
    fetchMock.mockImplementationOnce(async () =>
      response({ detail: 'internal authentication detail' }, 401),
    );
    renderLogin();
    fireEvent.change(screen.getByLabelText('Username'), {
      target: { value: 'unknown-user' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'wrong-password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));

    expect((await screen.findByRole('alert')).textContent)
      .toContain('Invalid username or password.');
    expect(screen.queryByText('internal authentication detail')).toBeNull();
    expect(hasToken()).toBe(false);
  });

  it('shows approval records but no decision controls to read-only operators', async () => {
    renderApprovals(['read']);
    fireEvent.click(screen.getByText('apr-test'));

    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(screen.getByText(
      'This account is read-only and cannot submit approval decisions.',
    )).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Approve & execute' }))
      .toBeNull();
    expect(screen.queryByLabelText('Remediation token')).toBeNull();
  });

  it('shows decision controls only when remediation permission is present', async () => {
    renderApprovals(['read', 'remediation']);
    fireEvent.click(screen.getByText('apr-test'));

    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Approve & execute' }))
      .toBeTruthy();
    expect(screen.queryByLabelText('Approval token')).toBeNull();
    expect(screen.queryByLabelText('Remediation token')).toBeNull();
  });

  it('does not let the requester approve their own request', async () => {
    renderApprovals(
      ['read', 'remediation'],
      'PENDING',
      undefined,
      'test-operator',
    );
    fireEvent.click(screen.getByText('apr-test'));

    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(screen.getByRole('alert').textContent)
      .toContain('a different authorized operator must approve');
    expect((screen.getByRole('button', {
      name: 'Approve & execute',
    }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Reject' })).toBeTruthy();
  });

  it('allows the configured owner to approve their own request', async () => {
    renderApprovals(
      ['owner', 'read', 'remediation'],
      'PENDING',
      undefined,
      'test-operator',
    );
    fireEvent.click(screen.getByText('apr-test'));

    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(screen.getByText(/trusted owner account may approve its own request/))
      .toBeTruthy();
    const approveButton = screen.getByRole('button', {
      name: 'Approve & execute',
    }) as HTMLButtonElement;
    expect(approveButton.disabled).toBe(false);
    fireEvent.click(approveButton);
    expect(screen.queryByLabelText('Remediation token')).toBeNull();
  });

  it('does not offer a separate execution call for an approved request', async () => {
    renderApprovals(['read', 'remediation'], 'APPROVED', 'awaiting_execution');
    fireEvent.click(screen.getByRole('tab', { name: 'approved' }));
    fireEvent.click(screen.getByText('apr-test'));

    expect(await screen.findByRole('dialog')).toBeTruthy();
    expect(screen.getByText(/review the audit history for its outcome/i)).toBeTruthy();
    expect(screen.queryByRole('button', {
      name: 'Execute approved remediation',
    })).toBeNull();
    expect(screen.queryByLabelText('Approval token')).toBeNull();
  });

  it('clears the in-memory session on logout', async () => {
    renderApp();
    fireEvent.change(screen.getByLabelText('Username'), {
      target: { value: 'read-only' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'test-password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByRole('button', { name: 'Account menu' });
    fireEvent.click(screen.getByRole('button', { name: 'Account menu' }));
    fireEvent.click(screen.getByRole('button', { name: 'Log out' }));
    expect(await screen.findByLabelText('Username')).toBeTruthy();
    expect(hasToken()).toBe(false);
    expect(window.sessionStorage.getItem('kybernaut.operator-session')).toBeNull();
  });

  it('returns to login and clears the token when an authenticated request returns 401', async () => {
    renderApp();
    fireEvent.change(screen.getByLabelText('Username'), {
      target: { value: 'read-only' },
    });
    fireEvent.change(screen.getByLabelText('Password'), {
      target: { value: 'test-password' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    await screen.findByRole('button', { name: 'Account menu' });
    expect(hasToken()).toBe(true);
    fetchMock.mockImplementation(async (input: RequestInfo | URL) =>
      String(input).endsWith('/expired') ? response({}, 401) : response({}),
    );
    await expect(apiRequest('/expired')).rejects.toThrow(
      'Authentication is missing or expired. Sign in again.',
    );
    expect(await screen.findByLabelText('Username')).toBeTruthy();
    expect(hasToken()).toBe(false);
  });
});