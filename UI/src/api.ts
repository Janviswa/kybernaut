import { config } from './config';

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status?: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

let token = '';
let activeDemoSessionId: string | null = null;
const OPERATOR_SESSION_KEY = 'kybernaut.operator-session';

type OperatorSession = {
  accessToken: string;
  operatorId: string;
  permissions: string[];
  expiresAt: number;
};

export const setToken = (value: string) => {
  token = value;
};

export const persistOperatorSession = (session: OperatorSession) => {
  if (typeof window !== 'undefined') {
    window.sessionStorage.setItem(OPERATOR_SESSION_KEY, JSON.stringify(session));
  }
  token = session.accessToken;
};

export const restoreOperatorSession = (): Omit<OperatorSession, 'accessToken'> | null => {
  if (typeof window === 'undefined') return null;
  const raw = window.sessionStorage.getItem(OPERATOR_SESSION_KEY);
  if (!raw) {
    token = '';
    return null;
  }

  let session: unknown;
  try {
    session = JSON.parse(raw);
  } catch {
    clearToken();
    return null;
  }
  if (!isRecord(session) ||
    typeof session.accessToken !== 'string' ||
    !session.accessToken ||
    typeof session.operatorId !== 'string' ||
    !session.operatorId.trim() ||
    !Array.isArray(session.permissions) ||
    !session.permissions.length ||
    !session.permissions.every(isOperatorPermission) ||
    typeof session.expiresAt !== 'number' ||
    !Number.isFinite(session.expiresAt) ||
    session.expiresAt <= Date.now()) {
    clearToken();
    return null;
  }

  token = session.accessToken;
  return {
    operatorId: session.operatorId,
    permissions: session.permissions,
    expiresAt: session.expiresAt,
  };
};

export const clearToken = () => {
  token = '';
  if (typeof window !== 'undefined') {
    window.sessionStorage.removeItem(OPERATOR_SESSION_KEY);
  }
};

export const stopActiveDemoSession = () => {
  const sessionId = activeDemoSessionId;
  activeDemoSessionId = null;
  if (sessionId) void leaveDemoSession(sessionId);
};

export const hasToken = () => Boolean(token);

/** Authenticated binary download (used for the cost-report PDF). */
export async function apiBlob(path: string): Promise<Blob> {
  const response = await fetch(
    `${config.apiBase.replace(/\/+$/, '')}${path}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} },
  );
  if (response.status === 401 && token) window.dispatchEvent(new Event('kb-unauthorized'));
  if (!response.ok) {
    throw new ApiError(
      response.status === 403
        ? 'Your account is not permitted to perform this request.'
        : response.status === 404
          ? 'No cost report has been generated yet.'
          : `The download failed (HTTP ${response.status}).`,
      response.status,
    );
  }
  return response.blob();
}

export async function apiRequest<T>(
  path: string,
  options: RequestInit = {},
  suppliedToken = token,
): Promise<T> {
  const controller = new AbortController();
  const startedAt = performance.now();
  let failureLogged = false;
  const logFailure = (status: number | undefined, message: string) => {
    if (failureLogged) return;
    failureLogged = true;
    console.warn('[Kybernaut API] Request failed', {
      method: options.method || 'GET',
      path: path.split('?')[0],
      status: status ?? 'network',
      durationMs: Math.round(performance.now() - startedAt),
      message,
    });
  };
  const timeout = window.setTimeout(
    () => controller.abort(),
    config.timeoutMs,
  );
  const headers = new Headers(options.headers);
  headers.set('Accept', 'application/json');
  if (options.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  if (suppliedToken) headers.set('Authorization', `Bearer ${suppliedToken}`);

  try {
    const response = await fetch(
      `${config.apiBase.replace(/\/+$/, '')}${path}`,
      { ...options, headers, signal: controller.signal },
    );
    if (response.status === 401 && suppliedToken) {
      window.dispatchEvent(new Event('kb-unauthorized'));
    }
    if (!response.ok) {
      let detail = '';
      if (
        response.status === 403 || response.status === 409
        ||                 response.status === 400 || response.status === 502
      ) {
        try {
          const body = await response.clone().json();
          if (body && typeof body.detail === 'string') detail = body.detail.slice(0, 300);
        } catch { /* body is not JSON */ }
      }
      const message = detail && response.status !== 401
        ? detail
        : response.status === 401
        ? 'Authentication is missing or expired. Sign in again.'
        : response.status === 403
          ? 'Your account is not permitted to perform this request.'
          : `The API request failed (HTTP ${response.status}).`;
      logFailure(response.status, message);
      throw new ApiError(message, response.status);
    }
    if (response.status === 204) return undefined as T;
    try {
      return await response.json() as T;
    } catch {
      const message = 'The API returned an invalid JSON response.';
      logFailure(response.status, message);
      throw new ApiError(message, response.status);
    }
  } catch (error) {
    if (error instanceof ApiError) {
      logFailure(error.status, error.message);
      throw error;
    }
    if (error instanceof DOMException && error.name === 'AbortError') {
      const message = 'The API request timed out.';
      logFailure(undefined, message);
      throw new ApiError(message);
    }
    const message = 'The backend is unavailable. Check the API URL and connection.';
    logFailure(undefined, message);
    throw new ApiError(message);
  } finally {
    window.clearTimeout(timeout);
  }
}

export const loginOperator = (username: string, password: string) =>
  apiRequest<unknown>(
    '/auth/login',
    {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    },
    '',
  ).catch(error => {
    if (error instanceof ApiError && error.status === 401) {
      throw new ApiError('Invalid username or password.', 401);
    }
    if (error instanceof ApiError && error.status === 422) {
      throw new ApiError('Enter a valid username and password.', 422);
    }
    throw error;
  });

export const getHealth = () =>
  apiRequest<unknown>('/health', {}, '').then(response => {
    if (!isRecord(response) || typeof response.status !== 'string' ||
      typeof response.service !== 'string' || typeof response.version !== 'string' ||
      typeof response.checked_at !== 'string' || !isRecord(response.components)) {
      throw new ApiError('The backend returned an unexpected health response.');
    }
    const componentNames = ['api', 'prometheus', 'kubernetes_lab', 'database'];
    const validStatuses = ['healthy', 'degraded', 'unhealthy', 'unknown'];
    const components = Object.fromEntries(componentNames.map(name => {
      const component = response.components[name];
      if (!isRecord(component) ||
        typeof component.status !== 'string' ||
        !validStatuses.includes(component.status) ||
        typeof component.checked_at !== 'string' ||
        typeof component.reason !== 'string') {
        throw new ApiError('The backend returned an unexpected health component.');
      }
      return [name, component];
    }));
    return {
      status: response.status,
      service: response.service,
      version: response.version,
      checked_at: response.checked_at,
      components,
    };
  });

export const getIncidents = async () => {
  const response = await apiRequest<unknown>(
    '/incidents/history?source=operational',
  );
  if (!isRecord(response) || !Array.isArray(response.incidents)) {
    throw new ApiError('The backend returned an unexpected incident response.');
  }
  return response as { incidents: unknown[] };
};

export const getIncident = async (incidentId: string) => {
  try {
    const response = await apiRequest<unknown>(
      `/incidents/${encodeURIComponent(incidentId)}`,
    );
    if (!isRecord(response) || response.incident_id !== incidentId) {
      throw new ApiError('The backend returned an unexpected incident response.');
    }
    return response;
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      throw new ApiError('Incident not found in persisted records.', 404);
    }
    throw error;
  }
};

export const getIncidentTimeline = async (incidentId: string) => {
  const response = await apiRequest<unknown>(
    `/incidents/${encodeURIComponent(incidentId)}/timeline`,
  );
  if (!isRecord(response) || response.incident_id !== incidentId ||
    !Array.isArray(response.events) || !response.events.every(isRecord)) {
    throw new ApiError('The backend returned an unexpected incident timeline.');
  }
  return response as { incident_id: string; events: Record<string, unknown>[] };
};

export const getDemoIncidents = async () => {
  const response = await apiRequest<unknown>('/incidents/history?source=demo');
  if (!isRecord(response) || !Array.isArray(response.incidents)) {
    throw new ApiError('The backend returned an unexpected demo incident response.');
  }
  return response.incidents.filter(isRecord);
};

export const getDemoStatus = async () => {
  const response = await apiRequest<unknown>('/demo/status');
  if (!isRecord(response) || typeof response.enabled !== 'boolean' ||
    typeof response.status !== 'string') {
    throw new ApiError('The backend returned an unexpected demo status response.');
  }
  return response;
};

export const enterDemoSession = async (sessionId: string) => {
  const response = await apiRequest<unknown>('/demo/session', {
    method: 'POST',
    body: JSON.stringify({ session_id: sessionId }),
  });
  if (!isRecord(response) || typeof response.enabled !== 'boolean' ||
    typeof response.status !== 'string') {
    throw new ApiError('The backend returned an unexpected demo session response.');
  }
  activeDemoSessionId = response.enabled ? sessionId : null;
  return response;
};

export const heartbeatDemoSession = (sessionId: string) =>
  apiRequest<unknown>('/demo/session', {
    method: 'PUT',
    body: JSON.stringify({ session_id: sessionId }),
  });

export const leaveDemoSession = (sessionId: string) =>
  apiRequest<unknown>('/demo/session', {
    method: 'DELETE',
    body: JSON.stringify({ session_id: sessionId }),
    keepalive: true,
  });


export const getDiagnosis = async (service: string) => {
  const response = await apiRequest<unknown>(`/diagnose/${encodeURIComponent(service)}`);
  if (!isRecord(response) || response.service !== service ||
    typeof response.status !== 'string' || !Array.isArray(response.findings)) {
    throw new ApiError('The backend returned an unexpected diagnosis response.');
  }
  return response;
};

export const getServiceHealth = async (service: string) => {
  const response = await apiRequest<unknown>(
    `/service-health/${encodeURIComponent(service)}`,
  );
  if (!isRecord(response) || response.service !== service ||
    typeof response.health_status !== 'string' ||
    typeof response.freshness_status !== 'string' ||
    !Array.isArray(response.findings)) {
    throw new ApiError('The backend returned an unexpected service health response.');
  }
  return response;
};

export const getConfiguredServices = async () => {
  const response = await apiRequest<unknown>('/services');
  if (!isRecord(response) || !Array.isArray(response.services) ||
    !response.services.every(service => typeof service === 'string')) {
    throw new ApiError('The backend returned an unexpected configured services response.');
  }
  return response as { services: string[] };
};

export const getApprovals = async () => {
  const response = await apiRequest<unknown>('/approvals?limit=100');
  if (!isRecord(response) || !Array.isArray(response.approvals)) {
    throw new ApiError('The backend returned an unexpected approvals response.');
  }
  return response as { approvals: unknown[] };
};

export const getAuditEvents = async (
  incidentId?: string,
  limit = 100,
  source: 'all' | 'operational' | 'demo' = 'all',
) => {
  const query = new URLSearchParams({ limit: String(limit) });
  if (incidentId) query.set('incident_id', incidentId);
  if (source !== 'all') query.set('source', source);
  const response = await apiRequest<unknown>(`/audit?${query.toString()}`);
  if (!isRecord(response) || !Array.isArray(response.events)) {
    throw new ApiError('The backend returned an unexpected audit response.');
  }
  return response as { events: unknown[] };
};

export const getLiveTelemetry = async () => {
  const response = await apiRequest<unknown>('/telemetry');
  if (!isRecord(response) || response.source !== 'prometheus' ||
    typeof response.observed_at !== 'string' || !isRecord(response.services) ||
    !Array.isArray(response.unavailable_services)) {
    throw new ApiError('The backend returned an unexpected telemetry response.');
  }
  return response as {
    source: 'prometheus';
    observed_at: string;
    services: Record<string, unknown>;
    unavailable_services: string[];
  };
};

export const getCostUsage = async () => {
  const response = await apiRequest<unknown>('/cost/usage');
  if (!isRecord(response) || response.source !== 'prometheus' ||
    response.currency !== 'USD' ||
    !['estimated', 'rates_not_configured', 'usage_incomplete'].includes(String(response.status)) ||
    !isRecord(response.pricing) || typeof response.pricing.configured !== 'boolean' ||
    !isRecord(response.services) || !isRecord(response.summary) ||
    !Array.isArray(response.unavailable_services) ||
    typeof response.disclaimer !== 'string') {
    throw new ApiError('The backend returned an unexpected usage cost response.');
  }
  return response;
};

export const getIncidentMetrics = () =>
  apiRequest<unknown>('/metrics/incidents').then(response => {
    if (!isRecord(response)) throw new ApiError('The backend returned an unexpected incident metrics response.');
    return response;
  });

export const getLifecycleCounts = async () => {
  const metrics = await getIncidentMetrics();
  if (!isRecord(metrics) ||
    typeof metrics.active_incidents !== 'number' ||
    !Number.isFinite(metrics.active_incidents)) {
    throw new ApiError('The backend returned unexpected incident lifecycle counts.');
  }

  let pendingApprovals = metrics.pending_approvals;
  if (typeof pendingApprovals !== 'number' || !Number.isFinite(pendingApprovals)) {
    const response = await apiRequest<unknown>(
      '/approvals?status=PENDING&limit=1000',
    );
    if (!isRecord(response) || !Array.isArray(response.approvals)) {
      throw new ApiError('The backend returned unexpected pending approvals.');
    }
    pendingApprovals = response.approvals.filter(
      approval => isRecord(approval) && approval.status === 'PENDING',
    ).length;
  }
  return {
    active_incidents: metrics.active_incidents,
    pending_approvals: pendingApprovals,
  };
};

export const getMonitorStatus = () =>
  apiRequest<unknown>('/monitor/status').then(response => {
    if (!isRecord(response)) throw new ApiError('The backend returned an unexpected monitor status response.');
    return response;
  });

export const getCostReport = () =>
  apiRequest<unknown>('/cost/report').then(response => {
    if (!isRecord(response)) throw new ApiError('The backend returned an unexpected cost report response.');
    return response;
  }).catch(error => {
    if (error instanceof ApiError && error.status === 404) {
      throw new ApiError('No cost report has been generated yet.', 404);
    }
    throw error;
  });

export const runAdvisoryWorkflow = async (service: string) => {
  const result = await apiRequest<unknown>(
    `/workflow/${encodeURIComponent(service)}`,
  );
  if (!isRecord(result) || result.service !== service || !isRecord(result.policy)) {
    throw new ApiError('The backend returned an unexpected advisory response.');
  }
  return result;
};

export const decideApproval = (
  id: string,
  decision: 'approved' | 'rejected',
  reason: string,
) =>
  apiRequest<Record<string, unknown>>(
    `/approvals/${encodeURIComponent(id)}/decision`,
    {
      method: 'POST',
      body: JSON.stringify({ decision, reason }),
    },
  );

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function isOperatorPermission(
  value: unknown,
): value is 'owner' | 'read' | 'remediation' {
  return value === 'owner' || value === 'read' || value === 'remediation';
}

/* ---------- saved Console investigations (investigation history) ---------- */
export type InvestigationSummary = {
  investigation_id: string; operator_id: string; incident_id: string | null;
  title: string; message_count: number; created_at: string; updated_at: string;
  incident_status?: string | null; resolution_reason?: string | null;
};
export const listInvestigations = async () => {
  const response = await apiRequest<unknown>('/investigations');
  if (!isRecord(response) || !Array.isArray(response.investigations)) {
    throw new ApiError('The backend returned an unexpected investigation list.');
  }
  return response.investigations as InvestigationSummary[];
};
export const getInvestigation = async (id: string) => {
  const response = await apiRequest<unknown>(`/investigations/${encodeURIComponent(id)}`);
  if (!isRecord(response) || !Array.isArray(response.messages)) {
    throw new ApiError('The backend returned an unexpected investigation.');
  }
  return response as unknown as InvestigationSummary & { messages: Record<string, unknown>[] };
};
export const deleteInvestigation = (id: string) =>
  apiRequest<{ investigation_id: string; deleted: boolean }>(
    `/investigations/${encodeURIComponent(id)}`, { method: 'DELETE' });
export const saveInvestigation = (
  id: string,
  body: { title: string; incident_id: string | null; messages: unknown[] },
) => apiRequest<InvestigationSummary>(`/investigations/${encodeURIComponent(id)}`, {
  method: 'PUT', body: JSON.stringify(body),
});

/* ---------- agent + lab automation status ---------- */
export const getAgentStatus = (probe = false) =>
  apiRequest<Record<string, any>>(`/agent/status${probe ? '?probe=true' : ''}`);
export const getAutoIncidentStatus = () =>
  apiRequest<Record<string, any>>('/lab/auto-incidents');
export const getLocalServiceSimulationStatus = () =>
  apiRequest<Record<string, any>>('/local-service-simulation');
export const runLocalServiceSimulationNow = () =>
  apiRequest<Record<string, any>>('/local-service-simulation/run-now', { method: 'POST' });

/* ---------- remediation options + grounded Console answers ---------- */
export const getRemediationOptions = (incidentId: string) =>
  apiRequest<Record<string, any>>(`/incidents/${encodeURIComponent(incidentId)}/remediation-options`);
export const executeLocalSimulation = (incidentId: string) =>
  apiRequest<Record<string, any>>(
    `/incidents/${encodeURIComponent(incidentId)}/execute-local-simulation`,
    { method: 'POST' },
  );
export const executePaymentComposeRemediation = (incidentId: string) =>
  apiRequest<Record<string, any>>(
    `/incidents/${encodeURIComponent(incidentId)}/execute-payment-compose-remediation`,
    {
      method: 'POST',
      body: JSON.stringify({ confirm_write_pause: true }),
    },
  );
export const askConsole = (body: { question: string; incident_id?: string | null; service?: string | null }) =>
  apiRequest<Record<string, any>>('/console/ask', { method: 'POST', body: JSON.stringify(body) });