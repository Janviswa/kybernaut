import { Fragment } from 'react';
import { Link } from 'react-router-dom';
import { ArrowDown, CheckCircle2, CircleAlert, CircleDashed, FlaskConical, ShieldAlert } from 'lucide-react';
import { Badge } from './ui';
import { fmt } from './data';
import './consoleCards.css';

type Rec = Record<string, unknown>;
const rec = (value: unknown): Rec | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Rec : null;
const text = (value: unknown, fallback = '—') =>
  value === null || value === undefined || value === '' ? fallback : String(value);
const label = (value: unknown) => text(value, '').replace(/_/g, ' ');

/* ====================================================================== */
/* Service health, explained for ONE service (the one that has the incident) */
/* ====================================================================== */

export type ServiceHealthData = {
  service: string;
  health: Rec | null;
  healthError?: string;
  telemetry: Rec | null;
  incident: Rec | null;
  otherOpen: Rec[];
};

const ageSeconds = (iso: unknown) => {
  const time = typeof iso === 'string' ? Date.parse(iso) : Number.NaN;
  return Number.isFinite(time) ? Math.max(0, Math.round((Date.now() - time) / 1000)) : null;
};
const bytes = (value: unknown) => {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 'Unavailable';
  return value >= 1024 ** 3 ? `${(value / 1024 ** 3).toFixed(2)} GiB`
    : `${(value / 1024 ** 2).toFixed(1)} MiB`;
};
const num = (value: unknown, digits: number, unit = '') =>
  typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(digits)}${unit}` : 'Unavailable';

export function ServiceHealthCard({ data }: { data: ServiceHealthData }) {
  const { service, health, healthError, telemetry, incident, otherOpen } = data;
  const status = String(health?.health_status ?? 'unknown');
  const freshness = String(health?.freshness_status ?? 'unknown');
  const findings = (Array.isArray(health?.findings) ? health!.findings as unknown[] : [])
    .map(rec).filter((item): item is Rec => item !== null && item.type !== 'no_detected_anomaly');
  const verdict = !health
    ? 'unknown'
    : status === 'healthy' && freshness === 'fresh' ? 'healthy'
      : status === 'unhealthy' ? 'unhealthy'
        : status === 'degraded' ? 'degraded'
          : freshness === 'stale' ? 'stale' : 'unknown';
  const incidentFacts = Array.isArray(rec(incident?.source_evidence)?.evidence)
    ? (rec(incident?.source_evidence)!.evidence as unknown[]).filter((x): x is string => typeof x === 'string')
    : [];

  let explanation: string;
  if (!health) {
    explanation = `Live health for ${service} could not be read${healthError ? ` (${healthError})` : ''}. Unavailable is not the same as healthy.`;
  } else if (verdict === 'healthy') {
    explanation = incident
      ? `Prometheus metrics for ${service} look normal and are fresh. The open incident is not visible in these metrics, so it is most likely an application-level fault (for example an injected or functional failure) rather than a resource problem.`
      : `Prometheus metrics for ${service} look normal and are fresh. No anomaly was detected.`;
  } else if (verdict === 'stale' || verdict === 'unknown') {
    explanation = `The latest metrics for ${service} are ${verdict === 'stale' ? 'stale' : 'incomplete'}, so health cannot be confirmed right now.`;
  } else {
    explanation = `${service} is ${verdict}. ${findings.length
      ? `Detected: ${findings.map(f => label(f.type)).join(', ')}.` : 'See the evidence below.'}`;
  }

  const age = ageSeconds(health?.checked_at);
  return <div className="ai hcard" aria-label={`Service health for ${service}`}>
    <div className="row between gap">
      <b>Service health · {service}</b>
      <Badge v={verdict === 'unknown' || verdict === 'stale' ? 'pending' : verdict}>{verdict.toUpperCase()}</Badge>
    </div>
    <p className="hcard-explain">{explanation}</p>

    {incident && <div className="hcard-incident">
      <ShieldAlert size={16} aria-hidden />
      <div>
        <b>Incident on this service</b>
        <p className="tiny">
          <Link to={`/console?incident=${encodeURIComponent(String(incident.incident_id))}`}>{text(incident.incident_id)}</Link>
          {' · '}{label(incident.incident_type)} · {text(incident.severity)} · {text(incident.status)}
        </p>
        {incident.message ? <p className="tiny muted">{text(incident.message)}</p> : null}
        {incidentFacts.length > 0 && <ul className="tiny">{incidentFacts.slice(0, 3).map((fact, i) => <li key={i}>{fact}</li>)}</ul>}
      </div>
    </div>}

    <div className="hcard-grid">
      <div><small>CPU</small><b>{num(telemetry?.cpu_cores_used, 4, ' cores')}</b></div>
      <div><small>Memory</small><b>{bytes(telemetry?.memory_bytes)}</b></div>
      <div><small>Request rate</small><b>{num(telemetry?.request_rate, 2, ' req/s')}</b></div>
      <div><small>P95 latency</small><b>{typeof telemetry?.p95_latency_seconds === 'number'
        ? `${(telemetry.p95_latency_seconds * 1000).toFixed(0)} ms` : 'Unavailable'}</b></div>
    </div>

    {findings.length > 0 && <ul className="tiny hcard-findings">
      {findings.map((finding, i) => <li key={i}><b>{label(finding.type)}</b>: {text(finding.evidence)}</li>)}
    </ul>}

    <p className="tiny muted">
      Source: Prometheus{health ? ` · freshness ${freshness}` : ''}{age !== null ? ` · checked ${age}s ago` : ''}.
      {otherOpen.length > 0 ? ` ${otherOpen.length} other open incident${otherOpen.length === 1 ? '' : 's'} on ${service}.` : ''}
      {' '}Read-only; nothing was changed.
    </p>
  </div>;
}

/* ====================================================================== */
/* Audit flow chart                                                       */
/* ====================================================================== */

const PHASES: [string, RegExp][] = [
  ['Detected', /detect|opened|created|anomal|simulat|observed|fault_injected/i],
  ['Investigated', /investigat|diagnos|rca|classif|option|assess|workflow/i],
  ['Approval', /approval|approved|propos|policy|request|decid/i],
  ['Executed', /execut|clear|restart|scale|remediat|action_|rollback/i],
  ['Verified', /verif|recover/i],
  ['Resolved', /resolv|closed|cleared_condition|condition_cleared/i],
];
const phaseOf = (title: string) => {
  if (title === 'local_service_simulation_cleared') return 'Executed';
  return PHASES.find(([, re]) => re.test(title))?.[0] ?? 'Other';
};

const toneOf = (title: string, outcome: string) => {
  const value = `${title} ${outcome}`.toLowerCase();
  if (/fail|error|reject|denied|block|invalid|unavailable/.test(value)) return 'bad';
  if (/approved|success|complete|resolved|verified|executed|recovered|cleared/.test(value)) return 'good';
  if (/pending|request|propos|await|expire/.test(value)) return 'warn';
  return 'info';
};

export const chronological = (events: Rec[]) =>
  events.map((event, index) => ({ event, index, time: typeof event.timestamp === 'string' ? Date.parse(event.timestamp) : Number.NaN }))
    .sort((a, b) => {
      const av = Number.isFinite(a.time); const bv = Number.isFinite(b.time);
      if (av !== bv) return av ? -1 : 1;
      return av && a.time !== b.time ? a.time - b.time : a.index - b.index;
    }).map(item => item.event);

type StageNode = {
  key: string;
  kind: 'event' | 'derived';
  title: string;
  tone: string;
  event?: Rec;
  outcome?: string;
  note?: string;
  time?: string | null;
};

export function AuditFlowCard({ events, incidentId, title = 'Audit flow', incident = null }: {
  events: Rec[]; incidentId?: string | null; title?: string; incident?: Rec | null;
}) {
  const ordered = chronological(events);
  if (!ordered.length) {
    return <div className="ai"><b>{title}</b><p className="muted">No audit events are recorded{incidentId ? ` for ${incidentId}` : ''} yet.</p></div>;
  }
  const nodes = ordered.map(event => {
    const name = text(event.event_type, 'event');
    const outcome = text(event.outcome, '');
    return { event, name, outcome, phase: phaseOf(name), tone: toneOf(name, outcome) };
  });
  const reached = new Set(nodes.map(n => n.phase));
  const simulatorResolved = nodes.some(
    node => node.name === 'local_service_simulation_cleared'
      && node.outcome === 'resolved',
  );
  if (simulatorResolved) {
    // This event is written only after the authorized local clear is verified.
    for (const phase of ['Detected', 'Investigated', 'Approval', 'Executed', 'Verified', 'Resolved']) {
      reached.add(phase);
    }
  }
  const first = Date.parse(String(ordered[0].timestamp));
  const last = Date.parse(String(ordered[ordered.length - 1].timestamp));
  const span = Number.isFinite(first) && Number.isFinite(last) && last > first
    ? (last - first < 60_000 ? `${Math.round((last - first) / 1000)}s` : `${Math.round((last - first) / 60_000)} min`) : null;

  // One entry per lifecycle stage: recorded events first, then (only where the
  // evidence proves the stage happened) a clearly-labelled derived entry.
  const cleared = nodes.find(node => node.name === 'local_service_simulation_cleared');
  const derivedNote: Record<string, string> = {
    Detected: incident?.opened_at
      ? `Opened ${fmt(String(incident.opened_at))}${incident.service ? ` on ${text(incident.service)}` : ''}${incident.severity ? ` · ${text(incident.severity)}` : ''}.`
      : 'The fault was detected before it could be cleared.',
    Investigated: 'The operator reviewed the incident and its scoped remediation.',
    Approval: 'The operator approved the action by selecting Execute; the API checked permission and incident state.',
    Executed: 'The recovery action was completed.',
    Verified: 'The service confirmed the fault is inactive.',
    Resolved: 'The incident was closed after the verified clear.',
  };
  const stages = PHASES.map(([name]) => {
    const real: StageNode[] = nodes.filter(node => node.phase === name).map((node, i) => ({
      key: `${name}-${i}`, kind: 'event', title: node.name.replace(/_/g, ' '), tone: node.tone,
      event: node.event, outcome: node.outcome,
    }));
    const stageReached = reached.has(name);
    const list = [...real];
    if (!real.length && stageReached) {
      const derivedFrom = name === 'Detected' && incident?.opened_at
        ? String(incident.opened_at) : (cleared?.event.timestamp as string | undefined) ?? null;
      list.push({
        key: `${name}-derived`, kind: 'derived', title: name, tone: 'good',
        note: derivedNote[name], time: derivedFrom,
      });
    }
    return { name, reached: stageReached, list };
  });
  const other = nodes.filter(node => node.phase === 'Other');
  const doneCount = stages.filter(stage => stage.reached).length;

  return <div className="ai aflow" aria-label={title}>
    <div className="row between gap">
      <b>{title}{incidentId ? ` · ${incidentId}` : ''}</b>
      <span className="tiny muted">{ordered.length} event{ordered.length === 1 ? '' : 's'}{span ? ` over ${span}` : ''} · {doneCount}/{PHASES.length} stages</span>
    </div>
    <ol className="aflow-phases" aria-label="Lifecycle stages">
      {PHASES.map(([name]) => <li key={name} className={reached.has(name) ? 'on' : ''}>
        {reached.has(name) ? <CheckCircle2 size={14} aria-hidden /> : <CircleDashed size={14} aria-hidden />}{name}
      </li>)}
    </ol>
    <ol className="aflow-stages">
      {stages.map((stage, stageIndex) => <li key={stage.name} className={`aflow-stage ${stage.reached ? 'is-on' : 'is-off'}`}
        style={{ animationDelay: `${stageIndex * 90}ms` }}>
        <span className="aflow-num" aria-hidden>{stage.reached ? <CheckCircle2 size={16} /> : stageIndex + 1}</span>
        <div className="aflow-body">
          <div className="aflow-head"><strong>{stage.name}</strong>
            {!stage.reached && <span className="tiny muted">Not reached yet</span>}</div>
          {stage.list.map(node => node.kind === 'event' && node.event
            ? <div key={node.key} className={`aflow-node t-${node.tone}`}>
              <div className="row between gap">
                <b>{node.title}</b>
                <span className="aflow-phase">Recorded</span>
              </div>
              <span className="tiny muted">
                {fmt(typeof node.event.timestamp === 'string' ? node.event.timestamp : null)}
                {' · '}{text(node.event.actor, 'unknown actor')}
                {node.event.resource ? ` · ${text(node.event.resource)}` : ''}
              </span>
              {node.outcome && <span className="tiny"> {node.outcome}</span>}
              {node.event.summary ? <p className="tiny muted">{text(node.event.summary)}</p> : null}
            </div>
            : <div key={node.key} className="aflow-derived">
              <div className="row between gap">
                <strong>{node.title}</strong>
                <span className="aflow-phase">Derived</span>
              </div>
              <p className="tiny muted">{node.note}{node.time ? ` · ${fmt(node.time)}` : ''}</p>
              <p className="tiny muted">Inferred from the verified clear event; no separate audit record exists for this stage.</p>
            </div>)}
        </div>
      </li>)}
    </ol>
    {other.length > 0 && <div className="aflow-other">
      <p className="tiny"><b>Other recorded events</b></p>
      {other.map((node, index) => <div key={index} className={`aflow-node t-${node.tone}`}>
        <b>{node.name.replace(/_/g, ' ')}</b>
        <span className="tiny muted"> {fmt(typeof node.event.timestamp === 'string' ? node.event.timestamp : null)} · {text(node.event.actor, 'unknown actor')}</span>
        {node.outcome && <span className="tiny"> {node.outcome}</span>}
      </div>)}
    </div>}
  </div>;
}

/* ====================================================================== */
/* Short execution report                                                 */
/* ====================================================================== */

export type ReportData = {
  title: string;
  status: 'resolved' | 'dry_run' | 'not_resolved' | 'failed';
  summary: string;
  rows: [string, string][];
  next?: string[];
};

export function ReportCard({ report }: { report: ReportData }) {
  const Icon = report.status === 'resolved' ? CheckCircle2
    : report.status === 'dry_run' ? FlaskConical : CircleAlert;
  return <section className={`ai report r-${report.status}`} aria-label="Execution report">
    <div className="row between gap">
      <b className="row gap"><Icon size={17} aria-hidden />{report.title}</b>
      <Badge v={report.status === 'resolved' ? 'resolved' : report.status === 'dry_run' ? 'pending' : 'failed'}>
        {report.status === 'resolved' ? 'RESOLVED' : report.status === 'dry_run' ? 'DRY RUN' : 'NOT RESOLVED'}
      </Badge>
    </div>
    <p>{report.summary}</p>
    <dl className="kvl">{report.rows.map(([k, v]) => <Fragment key={k}><dt>{k}</dt><dd>{v}</dd></Fragment>)}</dl>
    {report.next && report.next.length > 0 && <>
      <p className="tiny"><b>Next</b></p>
      <ul className="tiny">{report.next.map((item, i) => <li key={i}>{item}</li>)}</ul>
    </>}
  </section>;
}