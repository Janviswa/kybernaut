import { useSyncExternalStore } from 'react';
import {
  ApiError,
  clearToken,
  decideApproval as apiDecideApproval,
  getApprovals,
  getAuditEvents,
  getCostReport,
  getDiagnosis,
  getHealth,
  getIncidentMetrics,
  getIncidents,
  getLifecycleCounts,
  getMonitorStatus,
  isOperatorPermission,
  isRecord,
  loginOperator,
  persistOperatorSession,
  restoreOperatorSession,
  runAdvisoryWorkflow,
  stopActiveDemoSession,
} from './api';
import { SERVICES } from './data';

export type Resource<T> = {
  status: 'idle' | 'loading' | 'ready' | 'error' | 'unavailable';
  data: T | null;
  error: string | null;
};

const resource = <T,>(): Resource<T> => ({
  status: 'idle',
  data: null,
  error: null,
});

const restoredSession = restoreOperatorSession();

type ServiceResult = {
  service: string;
  status: 'ready' | 'error';
  data?: Record<string, unknown>;
  error?: string;
};

type State = {
  authenticated: boolean;
  operatorId: string | null;
  permissions: string[];
  sidebar: 'expanded' | 'collapsed';
  health: Resource<Record<string, unknown>>;
  incidents: Resource<Record<string, unknown>[]>;
  services: Resource<ServiceResult[]>;
  approvals: Resource<Record<string, unknown>[]>;
  lifecycleCounts: Resource<{
    active_incidents: number;
    pending_approvals: number;
  }>;
  audit: Resource<Record<string, unknown>[]>;
  metrics: Resource<Record<string, unknown>>;
  monitor: Resource<Record<string, unknown>>;
  cost: Resource<Record<string, unknown>>;
  investigations: Resource<never[]>;
};

let state: State = {
  authenticated: restoredSession !== null,
  operatorId: restoredSession?.operatorId ?? null,
  permissions: restoredSession?.permissions ?? [],
  sidebar: 'expanded',
  health: resource(),
  incidents: resource(),
  services: resource(),
  approvals: resource(),
  lifecycleCounts: resource(),
  audit: resource(),
  metrics: resource(),
  monitor: resource(),
  cost: resource(),
  investigations: {
    status: 'unavailable',
    data: null,
    error: 'The backend does not provide an investigation-history endpoint.',
  },
};

const listeners = new Set<() => void>();
const authListeners = new Set<() => void>();
const requestIds = new Map<string, number>();
let requestSequence = 0;

const publish = () => listeners.forEach(listener => listener());
const publishAuth = () => authListeners.forEach(listener => listener());
const replace = (next: Partial<State>) => {
  state = { ...state, ...next };
  publish();
};

export const useStore = () =>
  useSyncExternalStore(
    listener => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
  );

export const useAuth = () =>
  useSyncExternalStore(
    listener => {
      authListeners.add(listener);
      return () => authListeners.delete(listener);
    },
    () => state.authenticated,
  );

export const set = (next: Partial<State>) => replace(next);

export const toast = (message: string, kind = 'ok') =>
  window.dispatchEvent(new CustomEvent('kb-toast', {
    detail: { m: message, kind },
  }));

export async function signIn(username: string, password: string) {
  const cleanUsername = username.trim();
  if (!cleanUsername || !password) {
    throw new ApiError('Enter your username and password.');
  }
  const response = await loginOperator(cleanUsername, password);
  if (!isRecord(response) ||
    typeof response.access_token !== 'string' ||
    response.token_type !== 'bearer' ||
    typeof response.expires_in !== 'number' ||
    !Number.isFinite(response.expires_in) ||
    response.expires_in <= 0 ||
    typeof response.operator_id !== 'string' ||
    !Array.isArray(response.permissions) ||
    !response.permissions.length ||
    !response.permissions.every(isOperatorPermission)) {
    throw new ApiError('The backend returned an unexpected login response.');
  }
  persistOperatorSession({
    accessToken: response.access_token,
    operatorId: response.operator_id,
    permissions: response.permissions as string[],
    expiresAt: Date.now() + response.expires_in * 1000,
  });
  state = {
    ...state,
    authenticated: true,
    operatorId: response.operator_id,
    permissions: response.permissions as string[],
  };
  publishAuth();
  publish();
}

export function signOut() {
  stopActiveDemoSession();
  clearToken();
  requestIds.clear();
  state = {
    authenticated: false,
    operatorId: null,
    permissions: [],
    sidebar: state.sidebar,
    health: resource(),
    incidents: resource(),
    services: resource(),
    approvals: resource(),
    lifecycleCounts: resource(),
    audit: resource(),
    metrics: resource(),
    monitor: resource(),
    cost: resource(),
    investigations: {
      status: 'unavailable',
      data: null,
      error: 'The backend does not provide an investigation-history endpoint.',
    },
  };
  publishAuth();
  publish();
}

window.addEventListener('kb-unauthorized', signOut);

async function loadResource(key: keyof Pick<State,
  'health' | 'incidents' | 'services' | 'approvals' | 'audit' | 'metrics' | 'cost'
  | 'monitor' | 'lifecycleCounts'
>) {
  switch (key) {
    case 'health': return await getHealth() as Record<string, unknown>;
    case 'incidents': {
      const response = await getIncidents();
      if (!Array.isArray(response.incidents) || !response.incidents.every(item =>
        isRecord(item) && typeof item.incident_id === 'string' &&
        typeof item.service === 'string' && typeof item.status === 'string' &&
        typeof item.severity === 'string' && typeof item.incident_type === 'string',
      )) throw new ApiError('Unexpected incident response shape.');
      return response.incidents as Record<string, unknown>[];
    }
    case 'services': {
      const results = await Promise.all(SERVICES.map(async service => {
        try {
          return {
            service,
            status: 'ready' as const,
            data: await getDiagnosis(service),
          };
        } catch (error) {
          return {
            service,
            status: 'error' as const,
            error: error instanceof Error ? error.message : 'Diagnosis request failed.',
          };
        }
      }));
      return results;
    }
    case 'approvals': {
      const response = await getApprovals();
      if (!Array.isArray(response.approvals) || !response.approvals.every(item =>
        isRecord(item) && typeof item.approval_id === 'string' &&
        typeof item.status === 'string' && typeof item.requested_by === 'string',
      )) throw new ApiError('Unexpected approvals response shape.');
      return response.approvals as Record<string, unknown>[];
    }
    case 'lifecycleCounts':
      return await getLifecycleCounts();
    case 'audit': {
      const response = await getAuditEvents();
      if (!Array.isArray(response.events) || !response.events.every(item =>
        isRecord(item) && typeof item.event_type === 'string' &&
        typeof item.actor === 'string' && typeof item.timestamp === 'string',
      )) throw new ApiError('Unexpected audit response shape.');
      return response.events as Record<string, unknown>[];
    }
    case 'metrics': {
      const response = await getIncidentMetrics();
      if (!isRecord(response) ||
        typeof response.total_incidents !== 'number' ||
        typeof response.active_incidents !== 'number' ||
        !isRecord(response.by_status) ||
        !isRecord(response.by_severity) ||
        typeof response.resolved_incidents_with_duration !== 'number' ||
        (response.mttr_seconds !== null && typeof response.mttr_seconds !== 'number') ||
        typeof response.recurrence_rate !== 'number') {
        throw new ApiError('Unexpected incident metrics response shape.');
      }
      return response;
    }
    case 'monitor': {
      const response = await getMonitorStatus();
      if (!isRecord(response)) throw new ApiError('Unexpected monitor status response shape.');
      return response;
    }
    case 'cost': {
      const response = await getCostReport();
      if (!isRecord(response) || !isRecord(response.summary) || !Array.isArray(response.recommendations)) {
        throw new ApiError('Unexpected cost report response shape.');
      }
      return response;
    }
  }
}

export async function refresh(
  key: keyof Pick<State,
    'health' | 'incidents' | 'services' | 'approvals' | 'audit' | 'metrics' | 'cost'
    | 'monitor' | 'lifecycleCounts'
  >,
) {
  const requestId = ++requestSequence;
  requestIds.set(key, requestId);
  replace({ [key]: { ...state[key], status: 'loading', error: null } } as Partial<State>);
  try {
    const data = await loadResource(key);
    if (requestIds.get(key) !== requestId || (key !== 'health' && !state.authenticated)) return;
    replace({ [key]: { status: 'ready', data, error: null } } as Partial<State>);
  } catch (error) {
    if (requestIds.get(key) !== requestId || (key !== 'health' && !state.authenticated)) return;
    replace({
      [key]: {
        ...state[key],
        status: 'error',
        error: error instanceof Error ? error.message : 'The request failed.',
      },
    } as Partial<State>);
  }
}

export async function runWorkflow(service: string) {
  const result = await runAdvisoryWorkflow(service);
  await Promise.all([
    refresh('incidents'),
    refresh('approvals'),
    refresh('lifecycleCounts'),
  ]);
  return result;
}

export async function decide(
  id: string,
  decision: 'approved' | 'rejected',
  reason: string,
) {
  const result = await apiDecideApproval(
    id, decision, reason,
  );
  await Promise.all([
    refresh('approvals'),
    refresh('audit'),
    refresh('lifecycleCounts'),
  ]);
  return result;
}
