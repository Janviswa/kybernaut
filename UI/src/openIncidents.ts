import { SERVICES } from './data';

type Rec = Record<string, unknown>;

/** Incident states that are still unresolved. */
export const OPEN_STATUSES = ['OPEN', 'REMEDIATING'] as const;

const isDemoRecord = (incident: Rec) => {
  const evidence = incident.source_evidence;
  const simulation = evidence && typeof evidence === 'object'
    ? (evidence as Rec).simulation : undefined;
  return incident.source === 'demo' || simulation === 'demo';
};

const SEVERITY_ORDER = ['critical', 'high', 'medium', 'low'];
export const severityRank = (incident: Rec) => {
  const index = SEVERITY_ORDER.indexOf(String(incident.severity ?? '').toLowerCase());
  return index === -1 ? SEVERITY_ORDER.length : index;
};

export const isOpenIncident = (incident: Rec) =>
  (OPEN_STATUSES as readonly string[]).includes(String(incident.status ?? '').toUpperCase());

/**
 * The application incident list used by the left sidebar badge, notification bell,
 * and Console right-hand list. Kubernetes-lab history is shown separately.
 */
export function openIncidentsOf(records: Rec[] | null | undefined): Rec[] {
  return (records || []).filter(incident =>
    isOpenIncident(incident)
    && !isDemoRecord(incident)
    && (SERVICES as readonly string[]).includes(String(incident.service)))
    // Critical first, then high, medium, low. Array.sort is stable, so incidents of
    // the same severity keep the order the backend returned them in.
    .sort((a, b) => severityRank(a) - severityRank(b));
}

/**
 * Effective service state: the backend diagnosis, overridden by open incidents so that
 * a service with an active outage is never reported as up.
 */
export function effectiveServiceStatus(
  service: string, diagnosed: unknown, records: Rec[] | null | undefined,
): 'up' | 'degraded' | 'down' | 'unknown' {
  const open = openIncidentsOf(records).filter(item => item.service === service);
  if (open.some(item => String(item.incident_type ?? item.type ?? '') === 'service_unavailable'
    || String(item.severity ?? '').toLowerCase() === 'critical')) return 'down';
  if (String(diagnosed) === 'down') return 'down';
  if (open.length > 0 || String(diagnosed) === 'degraded') return 'degraded';
  return String(diagnosed) === 'up' ? 'up' : 'unknown';
}