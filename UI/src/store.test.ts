import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import {
  hasToken,
  restoreOperatorSession,
  setToken,
} from './api';
import {
  decide,
  refresh,
  set as setStore,
  signOut,
  useStore,
} from './store';

const response = (body: unknown, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => body,
});

describe('authenticated resource refresh lifecycle', () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  let currentState: ReturnType<typeof useStore> | undefined;

  beforeEach(() => {
    signOut();
    setToken('store-test-access-token');
    setStore({
      authenticated: true,
      operatorId: 'read-operator',
      permissions: ['read'],
    });
    fetchMock = vi.fn(async () => response({ incidents: [] }));
    vi.stubGlobal('fetch', fetchMock);
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    renderHook(() => {
      currentState = useStore();
      return currentState;
    });
  });

  afterEach(() => {
    signOut();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('completes the first authenticated fetch successfully', async () => {
    await act(async () => refresh('incidents'));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, options] = fetchMock.mock.calls[0];
    expect(new Headers((options as RequestInit).headers).has('Authorization'))
      .toBe(true);
    expect(currentState?.incidents).toEqual({
      status: 'ready',
      data: [],
      error: null,
    });
  });

  it('exits loading with a visible error on a failed request and succeeds on retry', async () => {
    fetchMock.mockResolvedValueOnce(response({ detail: 'forbidden' }, 403));

    await act(async () => refresh('incidents'));
    expect(currentState?.incidents.status).toBe('error');
    expect(currentState?.incidents.error).toContain('not permitted');
    expect(console.warn).toHaveBeenCalledWith(
      '[Kybernaut API] Request failed',
      expect.objectContaining({
        path: '/incidents/history',
        status: 403,
        durationMs: expect.any(Number),
      }),
    );
    expect(JSON.stringify(vi.mocked(console.warn).mock.calls))
      .not.toContain('store-test-access-token');

    await act(async () => refresh('incidents'));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(currentState?.incidents).toEqual({
      status: 'ready',
      data: [],
      error: null,
    });
  });

  it('does not let an older response overwrite a newer successful refresh', async () => {
    let resolveOlder!: (value: ReturnType<typeof response>) => void;
    fetchMock
      .mockImplementationOnce(() => new Promise(resolve => {
        resolveOlder = resolve;
      }))
      .mockResolvedValueOnce(response({ incidents: [{
        incident_id: 'inc-newer',
        service: 'order-service',
        status: 'OPEN',
        severity: 'high',
        incident_type: 'test',
      }] }));

    let older!: Promise<void>;
    await act(async () => {
      older = refresh('incidents');
      const newer = refresh('incidents');
      await newer;
    });
    await act(async () => {
      resolveOlder(response({ incidents: [] }));
      await older;
    });

    expect(currentState?.incidents.status).toBe('ready');
    expect(currentState?.incidents.data?.map(item => item.incident_id))
      .toEqual(['inc-newer']);
  });

  it('submits the approval without a shared token and refreshes lifecycle counts', async () => {
    fetchMock.mockImplementation(async (input, init) => {
      const url = String(input);
      if (url.includes('/approvals/apr-test/decision')) {
        return response({ approval_id: 'apr-test', status: 'APPROVED' });
      }
      if (url.includes('/approvals?')) return response({ approvals: [] });
      if (url.includes('/audit?')) return response({ events: [] });
      if (url.endsWith('/metrics/incidents')) {
        return response({ active_incidents: 2, pending_approvals: 0 });
      }
      return response({});
    });

    await act(async () => {
      await decide(
        'apr-test',
        'approved',
        'approved by operator',
      );
    });

    expect(fetchMock.mock.calls.some(([url]) =>
      String(url).endsWith('/metrics/incidents'),
    )).toBe(true);
    const decisionRequest = fetchMock.mock.calls.find(([url]) =>
      String(url).includes('/approvals/apr-test/decision'),
    );
    expect(new Headers(decisionRequest?.[1]?.headers).get('X-Approval-Token'))
      .toBeNull();
    expect(currentState?.lifecycleCounts).toEqual({
      status: 'ready',
      data: { active_incidents: 2, pending_approvals: 0 },
      error: null,
    });
  });

  it('restores a valid tab session before issuing the next authenticated request', async () => {
    signOut();
    window.sessionStorage.setItem('kybernaut.operator-session', JSON.stringify({
      accessToken: 'restored-session-test-token',
      operatorId: 'restored-operator',
      permissions: ['read'],
      expiresAt: Date.now() + 60_000,
    }));

    const restored = restoreOperatorSession();
    expect(restored?.operatorId).toBe('restored-operator');
    expect(hasToken()).toBe(true);
    setStore({
      authenticated: true,
      operatorId: restored?.operatorId ?? null,
      permissions: restored?.permissions ?? [],
    });
    await act(async () => refresh('incidents'));

    const [, options] = fetchMock.mock.calls[0];
    expect(new Headers((options as RequestInit).headers).has('Authorization'))
      .toBe(true);
    expect(currentState?.incidents.status).toBe('ready');
  });
});
