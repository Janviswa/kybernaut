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

/** Service availability follows the current Prometheus-backed diagnosis only. */
export function diagnosedServiceStatus(
  diagnosed: unknown,
): 'up' | 'degraded' | 'down' | 'unknown' {
  return diagnosed === 'up' || diagnosed === 'degraded' || diagnosed === 'down'
    ? diagnosed
    : 'unknown';
}