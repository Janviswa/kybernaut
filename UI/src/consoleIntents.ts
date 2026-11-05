import { SERVICES } from './data';

export type ConsoleIntentKind =
  | 'capabilities'
  | 'health'
  | 'health_all'
  | 'metrics'
  | 'highest_p95'
  | 'service_list'
  | 'kubernetes'
  | 'incident'
  | 'incident_list'
  | 'audit'
  | 'timeline'
  | 'incident_question'
  | 'recommendation'
  | 'ambiguous'
  | 'unsupported';

export type ConsoleIntent = {
  kind: ConsoleIntentKind;
  service?: string;
  incidentId?: string;
  count?: number;
};

const INCIDENT_ID = /\b(?:inc|demo)[-_][a-z0-9][a-z0-9._:-]*\b/i;
const INCIDENT_REFERENCE = /\bincident(?:\s+id)?\s+([a-z0-9][a-z0-9._:-]*)\b/i;
const NON_ID_INCIDENT_WORDS = new Set([
  'this', 'that', 'the', 'a', 'an', 'all', 'history', 'timeline', 'list',
]);
const CAPABILITIES = /\b(what can (?:you|kybernaut) do|what does kybernaut do|what is kybernaut|how does kybernaut work|capabilities|help me use kybernaut)\b/i;
const AUDIT = /\b(audit|audit trail|audit log|audit history|who approved|approval history)\b/i;
const TIMELINE = /\b(timeline|event timeline|incident history)\b/i;
const INCIDENT_QUESTION = /\bwhat happened\b|\bwhat triggered\b|\bwhy\b(?=.*\b(?:incident|alert|rca|root cause|remediation|fix|cause|memory|leak|it|this|that)\b)|\bhow\b(?=.*\b(?:incident|alert|memory|leak|fixed|fix|remediation|rca|root cause|cause|it|this|that)\b)|\bsummari[sz]e\b(?=.*\b(?:incident|rca|root cause|this|that)\b)|\broot cause\b|\brca\b|\bevidence\b|\bwhat should we do next\b|\bwhat do we do next\b|\bwhat next\b/i;
const SERVICE_LIST = /\b(?:what|which)\s+(?:configured\s+)?services?\b.*\b(?:available|configured|supported|enabled)\b|\b(?:list|show)\s+(?:the\s+)?(?:configured|supported|available)\s+services?\b|\bavailable services\b/i;
const UNSAFE_ACTION = /\b(search(ing)? (?:the )?(?:application )?logs?|log search|restart|stop|scale|delete|kill|execute remediation|remediate|repair)\b/i;
const KUBERNETES = /\b(kubernetes|k8s|cluster|namespace|pod|pods|deployment|deployments)\b/i;
const METRICS = /\b(cpu|memory|ram|latency|slow|performance|prometheus|metric|metrics|telemetry|request rate|throughput)\b/i;
const HIGHEST_P95 = /\b(?:highest|max(?:imum)?)\b.*\bp95\b|\bp95\b.*\b(?:highest|max(?:imum)?)\b/i;
const HEALTH = /\b(health|healthy|unhealthy|availability|available|outage|is .* up|status of)\b/i;
const RECOMMENDATION = /\b(recommend|recommendation|recommended|policy|proposed action|next steps|what should)\b/i;
const INCIDENTS = /\b(incident|incidents|alert|alerts|root cause|rca|caused this)\b/i;
const AUDIT_COUNT = /\b(?:last|latest|show|display|only)\s+(\d+)\s+audit\b(?:\s+only)?|\b(?:last|latest|show|display|only)\s+(\d+)\s+(?:audit\s+)?(?:events?|entries|records?)\b|\b(?:audit\s+)?(?:events?|entries|records?)\s+(?:last|latest)\s+(\d+)\b/i;
const ALL_SERVICES = /\b(?:all|every)\s+(?:configured\s+)?services?\b|\bservices?\s+(?:health\s+)?(?:all|every)\b/i;
const HEALTH_TARGET = /\b(?:health|healthy|status)\s+(?:of|for)\s+(?:the\s+)?([a-z0-9][a-z0-9 -]*?)(?:\s+health)?[?.!]*$/i;

const serviceIn = (text: string) => SERVICES.find(service =>
  new RegExp(
    `(^|[^a-z0-9-])${service.split('-').map(part =>
      part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'),
    ).join('[- ]')}([^a-z0-9-]|$)`,
    'i',
  )
    .test(text),
);

export function classifyConsoleIntent(input: string): ConsoleIntent {
  const query = input.trim();
  const matchedService = serviceIn(query);
  const targetMatch = query.match(HEALTH_TARGET);
  const capturedTarget = targetMatch?.[1]
    .trim()
    .replace(/\s+service$/i, '')
    .replace(/\s+/g, '-');
  const service = matchedService || capturedTarget;
  const explicitIncidentId = query.match(INCIDENT_ID)?.[0];
  const incidentReference = query.match(INCIDENT_REFERENCE)?.[1];
  const incidentId = explicitIncidentId ||
    (incidentReference && !NON_ID_INCIDENT_WORDS.has(incidentReference.toLowerCase())
      ? incidentReference
      : undefined);
  const countMatch = query.match(AUDIT_COUNT);
  const count = countMatch
    ? Number.parseInt(countMatch[1] || countMatch[2] || countMatch[3], 10)
    : undefined;

  if (CAPABILITIES.test(query)) return { kind: 'capabilities' };
  if (AUDIT.test(query)) return { kind: 'audit', service, incidentId, count };
  if (TIMELINE.test(query)) return { kind: 'timeline', incidentId };
  if (INCIDENT_QUESTION.test(query)) {
    return { kind: 'incident_question', service, incidentId };
  }
  if (UNSAFE_ACTION.test(query)) return { kind: 'unsupported', service };
  if (RECOMMENDATION.test(query)) return { kind: 'recommendation', service, incidentId };
  if (incidentId) return { kind: 'incident', service, incidentId };
  if (SERVICE_LIST.test(query)) return { kind: 'service_list' };
  if (KUBERNETES.test(query)) return { kind: 'kubernetes' };
  if (HIGHEST_P95.test(query)) return { kind: 'highest_p95', service };
  if (METRICS.test(query)) return { kind: 'metrics', service };
  if (HEALTH.test(query)) {
    return ALL_SERVICES.test(query)
      ? { kind: 'health_all' }
      : { kind: 'health', service };
  }
  if (INCIDENTS.test(query)) {
    if (/\b(latest|recent|list|show|all|open)\b/i.test(query)) {
      return { kind: 'incident_list' };
    }
    return { kind: 'incident', service };
  }
  if (service) return { kind: 'ambiguous', service };
  if (/\b(it|that|this|there)\b/i.test(query)) return { kind: 'ambiguous' };
  return { kind: 'unsupported' };
}
