import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import SettingsPage from './pages/Settings';
import { setToken } from './api';
import { set as setStore, signOut } from './store';

const response = (body: unknown) => ({
  ok: true,
  status: 200,
  json: async () => body,
});

describe('lab incident generator settings', () => {
  beforeEach(() => {
    signOut();
    setToken('test-token');
    setStore({
      authenticated: true,
      operatorId: 'operator',
      permissions: ['read'],
      health: { status: 'ready', data: null, error: null },
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/lab/auto-incidents')) {
        return response({
          enabled: true,
          running: true,
          cycle_in_progress: true,
          interval_seconds: 600,
          cycles: 0,
          last_scenario: 'memory-leak',
          last_attempt_at: '2026-11-02T13:37:33+00:00',
          rotation: [{ scenario: 'memory-leak', expected_severity: 'medium' }],
        });
      }
      return response({ llm: {} });
    }));
  });

  afterEach(() => {
    cleanup();
    signOut();
    vi.unstubAllGlobals();
  });

  it('shows the four-service simulator under Incident generator, without the old lab content', async () => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).endsWith('/local-service-simulation')) return response({ enabled: true, configured: true, running: true });
      return response({ llm: {} });
    }));
    render(<MemoryRouter><SettingsPage /></MemoryRouter>);
    expect(screen.queryByRole('tab', { name: 'Four-service simulator' })).toBeNull();
    fireEvent.click(await screen.findByRole('tab', { name: 'Incident generator' }));

    expect(await screen.findByText('enabled')).toBeTruthy();
    expect(screen.getByText(/Every 10 minutes two new incidents are created \(one critical and one high\)/)).toBeTruthy();
    expect(screen.getByText(/Use the button to create a pair immediately/)).toBeTruthy();
    expect(screen.queryByRole('heading', { name: 'Four-service simulator' })).toBeNull();
    expect(screen.queryByText('Automatic lab incidents')).toBeNull();
    expect(screen.queryByText('Scenarios in rotation')).toBeNull();
    expect(screen.queryByText('Cycles completed since API start')).toBeNull();
  });

  it('starts the simulator on demand from the Incident generator tab', async () => {
    setStore({ permissions: ['read', 'remediation'] });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/local-service-simulation')) return response({ enabled: true, configured: true, running: true });
      if (url.endsWith('/local-service-simulation/run-now')) {
        expect(init?.method).toBe('POST');
        return response({ incidents_created: 2 });
      }
      if (url.includes('/incidents/history')) return response({ incidents: [] });
      return response({ llm: {} });
    });
    vi.stubGlobal('fetch', fetchMock);
    render(<MemoryRouter><SettingsPage /></MemoryRouter>);
    fireEvent.click(await screen.findByRole('tab', { name: 'Incident generator' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Simulate incidents now' }));
    expect(await screen.findByText(/Created 2 incidents \(one critical and one high\)/)).toBeTruthy();
  });
});