import { ReactNode, useEffect, useState } from 'react';
import { Activity, ArrowRight, Clock, FileJson, FileText, ListChecks, ScrollText, Server, ShieldCheck, Stethoscope } from 'lucide-react';
import { useNavigate, useParams, Link } from 'react-router-dom';
import { Page, DataTable, Badge, Empty, ResourceMessage } from '../ui';
import { isOpenIncident } from '../openIncidents';
import { getIncidentTimeline } from '../api';
import { useIncident } from '../useIncident';
import { refresh, useStore } from '../store';
import { fmt, SERVICES } from '../data';
import { AnimatedTimeline, BackButton, eventsFromAudit } from '../components';
import '../incidentDetail.css';
import '../polish.css';
import { formatDuration } from '../data';

type Row = Record<string, unknown>;
const rec = (value: unknown): Row | null => (value && typeof value === 'object' && !Array.isArray(value) ? value as Row : null);
const text = (value: unknown, fallback = '—') => (value === null || value === undefined || value === '' ? fallback : String(value));
const label = (value: unknown) => text(value, 'unknown').replace(/_/g, ' ');
const localSimulation = (evidence: Row | null) =>
  evidence?.simulation === 'local_service_failure';
const kubernetesLabRecord = (row: Row, evidence: Row | null) =>
  row.service === 'kubernetes-lab' || evidence?.source === 'kubernetes';

function incidentCategory(row: Row, evidence: Row | null): string {
  const saved = evidence?.incident_category;
  if (typeof saved === 'string' && saved && saved !== 'unknown') return saved;

  const rawType = text(evidence?.type || row.incident_type, 'unknown');
  const facts = Array.isArray(evidence?.evidence) ? evidence.evidence.join(' ') : '';
  const searchable = `${facts} ${text(row.message, '')}`.toLowerCase();
  if (/imagepullbackoff|errimagepull/.test(searchable)) return 'deployment_failure';
  if (/oomkilled/.test(searchable)) return 'memory_leak';
  if (/evicted|ephemeral.storage|emptydir/.test(searchable)) return 'disk_full';
  if (/crashloopbackoff|back.off restarting|termination reason error/.test(searchable)) return 'pod_crash';
  if (rawType === 'deployment_unavailable_replicas') return 'deployment_failure';
  return rawType;
}

function affectedWorkload(row: Row, evidence: Row | null): string {
  if (typeof evidence?.workload === 'string' && evidence.workload) return evidence.workload;
  const resource = evidence?.resource;
  if (typeof resource === 'string' && resource) {
    if (evidence?.resource_kind === 'Deployment') return resource;
    if (evidence?.resource_kind === 'Pod') {
      return resource.replace(/-[a-z0-9]{8,10}-[a-z0-9]{5}$/, '');
    }
    return resource;
  }
  return text(row.service);
}

const cleanMessage = (value: unknown) =>
  text(value).replace(/^LOCAL SIMULATION:\s*/i, '');
const isLive = (row: Row) => ['OPEN', 'REMEDIATING'].includes(String(row.status));

export function Incidents() {
  const state = useStore();
  const nav = useNavigate();
  const [query, setQuery] = useState('');
  const [severity, setSeverity] = useState('all');
  const [status, setStatus] = useState('all');
  const [scope, setScope] = useState<'application' | 'all'>('application');
  const records = state.incidents.data || [];
  const applicationServices = new Set<string>(SERVICES);
  const excludedLabCount = records.filter(item =>
    kubernetesLabRecord(item, rec(item.source_evidence))).length;
  const rows = records.filter(item =>
    (scope === 'all' || applicationServices.has(String(item.service))) &&
    `${item.incident_id || ''} ${item.incident_type || ''} ${item.service || ''} ${item.message || ''} ${text(rec(item.source_evidence)?.workload || rec(item.source_evidence)?.resource, '')}`.toLowerCase().includes(query.toLowerCase()) &&
    (severity === 'all' || item.severity === severity) &&
    (status === 'all' || item.status === status));
  // New incidents arrive on their own (lab generator / monitors): keep the feed fresh.
  useEffect(() => {
    const timer = window.setInterval(() => void refresh('incidents'), 20_000);
    return () => window.clearInterval(timer);
  }, []);

  return <Page title="Incidents & Alerts" sub="Incidents for the four application services. Kubernetes lab history is available separately and is not attributed to an application service." actions={<BackButton />}>
    {state.incidents.status === 'ready' && records.length > 0 && <div className="pl-stats" aria-label="Incident summary">
      <div className="pl-stat"><span>Open now</span><b>{rows.filter(isLive).length}</b></div>
      <div className="pl-stat sev-critical"><span>Critical open</span><b>{rows.filter(row => isLive(row) && row.severity === 'critical').length}</b></div>
      <div className="pl-stat sev-high"><span>High open</span><b>{rows.filter(row => isLive(row) && row.severity === 'high').length}</b></div>
      <div className="pl-stat sev-ok"><span>Closed</span><b>{rows.filter(row => row.status === 'RESOLVED').length}</b></div>
    </div>}
    <div className="row gap wrap">
      <input aria-label="Search incidents" placeholder="Search incident records" value={query} onChange={event => setQuery(event.target.value)} />
      <select aria-label="Severity" value={severity} onChange={event => setSeverity(event.target.value)}><option value="all">All severities</option>{['critical', 'high', 'medium', 'low'].map(value => <option key={value}>{value}</option>)}</select>
      <select aria-label="Status" value={status} onChange={event => setStatus(event.target.value)}><option value="all">All statuses</option>{['OPEN', 'REMEDIATING', 'RESOLVED'].map(value => <option key={value}>{value}</option>)}</select>
      <select aria-label="Incident scope" value={scope} onChange={event => setScope(event.target.value as 'application' | 'all')}>
        <option value="application">Four application services</option>
        <option value="all">Include Kubernetes lab history</option>
      </select>
      <button className="btn" onClick={() => void refresh('incidents')}>Refresh</button>
    </div>
    {scope === 'application' && excludedLabCount > 0 &&
      <p className="tiny muted" role="note">
        {excludedLabCount} Kubernetes lab incident{excludedLabCount === 1 ? '' : 's'} hidden from this four-service view. They are historical lab records, not incidents on order, payment, user, or inventory.
      </p>}
    {state.incidents.status === 'ready' && records.length === 0 &&
      <p className="muted" role="note">No incidents are currently recorded. Nothing is generated to fill this page.</p>}
    {state.incidents.status !== 'ready'
      ? <ResourceMessage resource={state.incidents} retry={() => void refresh('incidents')} />
      : <DataTable rows={rows} onRow={(row: any) => nav(`/incidents/${encodeURIComponent(String(row.incident_id))}`)} empty="No persisted incidents." cols={[
        { k: 'incident_id', h: 'ID', r: (row: any) => <span className={`pl-idcell sev-${String(row.severity || 'unknown')}`}><i aria-hidden />
          <span className="pl-mono">{row.incident_id}</span></span> },
        { k: 'incident_type', h: 'Type', r: (row: Row) => {
          const evidence = rec(row.source_evidence);
          const category = incidentCategory(row, evidence);
          const observedType = text(evidence?.type || row.incident_type, 'unknown');
          return <div><b>{label(category)}</b>{category !== observedType && <small className="muted" style={{ display: 'block' }}>Observed: {label(observedType)}</small>}</div>;
        } },
        { k: 'severity', h: 'Severity', r: (row: any) => <Badge v={String(row.severity || 'unknown')} /> },
        { k: 'status', h: 'Status', r: (row: any) => <Badge v={String(row.status || 'unknown').toLowerCase()} /> },
        { k: 'service', h: 'Affected service / workload', r: (row: Row) => {
          const evidence = rec(row.source_evidence);
          const workload = affectedWorkload(row, evidence);
          if (applicationServices.has(String(row.service))) {
            return <div><b>{text(row.service)}</b>{workload !== row.service &&
              <small className="muted" style={{ display: 'block' }}>Workload: {workload}</small>}</div>;
          }
          return <div><b>{workload}</b><small className="muted" style={{ display: 'block' }}>
            {kubernetesLabRecord(row, evidence)
              ? 'Kubernetes lab history · not mapped to an application service'
              : `${text(row.service)} · not one of the four application services`}
          </small></div>;
        } },
        { k: 'message', h: 'Message', r: (row: any) => <span className="pl-msg" title={String(row.message || '')}>{cleanMessage(row.message)}</span> },
        { k: 'opened_at', h: 'Timeline', r: (row: any) => <div className="pl-times"><span>Opened {fmt(row.opened_at)}</span>
          <small className="muted">Last seen {fmt(row.last_seen_at)}</small></div> },
        { k: 'actions', h: 'Action', cls: 'col-sticky', r: (row: any) => isOpenIncident(row)
          ? <Link className="btn sm primary pl-cta" aria-label="Open in Console to Investigate" title="Open in Console to Investigate"
            to={`/console?incident=${encodeURIComponent(String(row.incident_id))}`}
            onClick={event => event.stopPropagation()}>Investigate<ArrowRight size={14} aria-hidden /></Link>
          : <Link className="btn sm pl-cta ghost" aria-label={`View record for ${String(row.incident_id)}`} title="Resolved incidents are not re-investigated"
            to={`/incidents/${encodeURIComponent(String(row.incident_id))}`}
            onClick={event => event.stopPropagation()}><FileText size={14} aria-hidden />View record</Link> },
      ]} />}
  </Page>;
}

export function IncidentDetail() {
  const { incidentId } = useParams();
  const store = useStore();
  const loaded = useIncident(incidentId);
  const incident = loaded.status === 'ready' &&
    loaded.incident?.incident_id === incidentId ? loaded.incident : null;
  const [timeline, setTimeline] = useState<{ status: string; events: Row[]; error: string | null }>({ status: 'idle', events: [], error: null });
  const [reload, setReload] = useState(0);

  useEffect(() => {
    let active = true;
    if (!incident) { setTimeline({ status: 'idle', events: [], error: null }); return () => { active = false; }; }
    setTimeline(previous => ({ ...previous, status: 'loading', error: null }));
    getIncidentTimeline(incidentId || '').then(response => {
      if (active) setTimeline({ status: 'ready', events: response.events, error: null });
    }).catch(error => {
      if (active) setTimeline({ status: 'error', events: [], error: error instanceof Error ? error.message : 'Timeline request failed.' });
    });
    return () => { active = false; };
  }, [incidentId, Boolean(incident), reload]);

  if (loaded.status === 'loading' || loaded.status === 'idle' ||
    (loaded.status === 'ready' && !incident)) {
    return <Page title="Incident" actions={<BackButton fallback="/incidents" />}><div className="panel muted" role="status"><span className="spinner" /> Loading backend data…</div></Page>;
  }
  if (loaded.status === 'error') {
    return <Page title="Incident" actions={<BackButton fallback="/incidents" />}><div className="panel" role="alert"><p className="err">{loaded.error}</p><button className="btn" onClick={loaded.reload}>Retry</button></div></Page>;
  }
  if (!incident) {
    return <Page title="Incident not found" sub="This identifier does not exist in persisted incident records." actions={<BackButton fallback="/incidents" />}><Link to="/incidents">Back to incidents</Link></Page>;
  }

  const evidence = rec(incident.source_evidence);
  const diagnosis = rec(incident.diagnosis);
  const trustedTarget = rec(diagnosis?.trusted_target);
  const policy = rec(diagnosis?.policy_recommendation);
  const id = String(incident.incident_id);
  const category = incidentCategory(incident, evidence);
  const isKubernetesLab = incident.service === 'kubernetes-lab' ||
    evidence?.source === 'kubernetes';
  const isLocalSimulation = localSimulation(evidence);
  const approvals = (store.approvals.data || []).filter(item => item.incident_ref === id);
  const recommendations = Array.isArray(diagnosis?.recommendations) ? diagnosis!.recommendations as unknown[] : [];
  const evidenceFacts = Array.isArray(evidence?.evidence)
    ? evidence.evidence.filter((fact): fact is string => typeof fact === 'string')
    : [];

  const openedMs = Date.parse(String(incident.opened_at || ''));
  const endMs = incident.resolved_at ? Date.parse(String(incident.resolved_at)) : Date.now();
  const durationLabel = Number.isFinite(openedMs) && Number.isFinite(endMs) && endMs >= openedMs
    ? formatDuration((endMs - openedMs) / 1000) : '—';
  const severityName = String(incident.severity || 'unknown');
  const statusName = String(incident.status || 'unknown');
  const extraFacts = evidence
    ? Object.entries(evidence).filter(([key, value]) =>
      !['description', 'evidence', 'recommendation'].includes(key)
      && (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean'))
    : [];
  const policyAction = text(policy?.action, isLocalSimulation
    ? 'Approval required before the fix runs'
    : 'Not evaluated yet');
  const stages: { icon: ReactNode; name: string; tone: string; value: ReactNode }[] = [
    { icon: <Stethoscope size={16} />, name: 'Diagnosis', tone: incident.diagnosis_status || evidence?.incident_category || isLocalSimulation ? 'on' : 'off',
      value: text(incident.diagnosis_status, evidence?.incident_category
        ? `${label(category)} · classified from stored evidence`
        : isLocalSimulation ? `${label(incident.incident_type)} on ${text(incident.service, 'the service')}`
          : 'Investigation not run yet') },
    { icon: <Activity size={16} />, name: 'RCA', tone: diagnosis?.root_cause_analysis_status || isLocalSimulation ? 'on' : 'off',
      value: <>{text(diagnosis?.root_cause_analysis_status, isLocalSimulation
        ? text(evidence?.description, text(incident.message, 'Service failure detected'))
        : 'Investigation not run yet')}{diagnosis?.root_cause_analysis_error ? ` · ${text(diagnosis.root_cause_analysis_error)}` : ''}</> },
    { icon: <ShieldCheck size={16} />, name: 'Policy', tone: policy?.action || isLocalSimulation ? 'on' : 'off', value: policyAction },
    { icon: <ListChecks size={16} />, name: 'Approval', tone: approvals.length || typeof policy?.requires_human_approval === 'boolean' ? 'on' : 'off',
      value: typeof policy?.requires_human_approval === 'boolean' ? (policy.requires_human_approval ? 'Human approval required' : 'Not required by policy') : approvals.length ? 'Approval request recorded' : 'No approval request yet' },
  ];

  const Card = ({ icon, title, actions, wide, children }: { icon: ReactNode; title: string; actions?: ReactNode; wide?: boolean; children: ReactNode }) =>
    <section className={`idet-card ${wide ? 'wide' : ''}`}>
      <header><h3><span className="idet-ic" aria-hidden>{icon}</span>{title}</h3>{actions}</header>
      <div className="idet-body">{children}</div>
    </section>;

  return <Page title={id} sub={text(incident.message, '')} actions={<>
    <BackButton fallback="/incidents" />
    {isOpenIncident(incident) &&
      <Link className="btn primary" to={`/console?incident=${encodeURIComponent(id)}`}>Open in Console to Investigate</Link>}
    {SERVICES.includes(String(incident.service) as typeof SERVICES[number])
      ? <Link className="btn" to={`/services/${encodeURIComponent(String(incident.service))}`}>Service health</Link>
      : isKubernetesLab
        ? <Badge v="info">Kubernetes lab workload</Badge>
        : null}
    <Badge v={severityName}>{severityName}</Badge>
    <Badge v={statusName.toLowerCase()}>{statusName}</Badge>
  </>}>
    {isKubernetesLab && <div className="panel" role="note">
      <div className="row between gap">
        <b>This is a Kubernetes lab incident, not an incident on one of the four application services.</b>
        <Link className="btn sm" to="/incidents">View four application services</Link>
      </div>
      <p className="tiny muted">This historical record remains available for Kubernetes lab workflows. It does not indicate that order-service, payment-service, user-service, or inventory-service is affected.</p>
    </div>}

    <div className={`idet-hero sev-${severityName}`}>
      <div className="idet-stat"><Server size={16} aria-hidden /><div><small>Affected service</small><b>{text(incident.service)}</b></div></div>
      <div className="idet-stat"><Clock size={16} aria-hidden /><div><small>Opened</small><b>{fmt(String(incident.opened_at || ''))}</b></div></div>
      <div className="idet-stat"><Clock size={16} aria-hidden /><div><small>Last seen</small><b>{fmt(String(incident.last_seen_at || ''))}</b></div></div>
      <div className="idet-stat"><Activity size={16} aria-hidden /><div><small>{incident.resolved_at ? 'Time to resolve' : 'Open for'}</small><b>{durationLabel}</b></div></div>
    </div>

    <div className="idet-grid">
      <Card icon={<ListChecks size={16} />} title="Incident details">
        <dl className="idet-kv">
          <dt>Type</dt><dd>{text(incident.incident_type)}</dd>
          <dt>Service</dt><dd>{text(incident.service)}</dd>
          <dt>Source</dt><dd>{isLocalSimulation ? 'Application service telemetry' : isKubernetesLab ? 'Kubernetes lab observation' : text(incident.source, 'Operational telemetry')}</dd>
          <dt>Namespace</dt><dd>{text(evidence?.namespace || trustedTarget?.namespace, 'Not recorded')}</dd>
          <dt>{evidence?.resource ? 'Resource' : trustedTarget?.deployment ? 'Mapped workload' : 'Resource'}</dt>
          <dd>{text(evidence?.resource || trustedTarget?.deployment, 'Not recorded')}</dd>
          <dt>Opened</dt><dd>{fmt(String(incident.opened_at || ''))}</dd>
          <dt>Last seen</dt><dd>{fmt(String(incident.last_seen_at || ''))}</dd>
          <dt>Resolved</dt><dd>{incident.resolved_at ? fmt(String(incident.resolved_at)) : 'Not resolved'}</dd>
          {incident.resolution_reason && <><dt>Resolution reason</dt><dd>{text(incident.resolution_reason).replace(/_/g, ' ')}</dd></>}
          <dt>Fingerprint</dt><dd className="mono idet-fp">{text(incident.fingerprint)}</dd>
        </dl>
      </Card>

      <Card icon={<FileJson size={16} />} title="Evidence">
        {evidence ? <>
          {typeof evidence.description === 'string' && <p className="idet-lead">{evidence.description}</p>}
          {evidenceFacts.length > 0 && <ul className="idet-facts">{evidenceFacts.map((fact, index) => <li key={index}>{fact}</li>)}</ul>}
          {extraFacts.length > 0 && <div className="idet-chips">{extraFacts.map(([key, value]) =>
            <span className="idet-chip" key={key}><small>{key.replace(/_/g, ' ')}</small>{String(value)}</span>)}</div>}
          <details className="idet-raw"><summary>Raw evidence (JSON)</summary><pre className="json">{JSON.stringify(evidence, null, 2)}</pre></details>
        </> : <Empty text="No source evidence is stored for this record." />}
      </Card>

      <Card icon={<Stethoscope size={16} />} title="Diagnosis · RCA · Remediation" wide>
        <ol className="idet-stages">
          {stages.map(stage => <li key={stage.name} className={`idet-stage ${stage.tone}`}>
            <span className="idet-dot" aria-hidden>{stage.icon}</span>
            <div><small>{stage.name}</small><div>{stage.value}</div></div>
          </li>)}
        </ol>
        <p className="idet-note"><b>Policy reason</b> · {text(policy?.reason, isLocalSimulation
          ? 'The remediation requires the remediation permission and your explicit approval.'
          : 'Policy evaluation runs during Console investigation.')}</p>
        {!diagnosis && !isLocalSimulation && <p className="muted">
          This record stores an observation, not an investigation result. Open it in the Console and select <b>Start investigation</b> to run RCA and policy evaluation; no remediation runs unless separately executed.
        </p>}
        {!diagnosis && typeof evidence?.recommendation === 'string' &&
          <p><b>Evidence-based recommendation:</b> {evidence.recommendation}</p>}
        {recommendations.length > 0 && <><h3>Recommendations</h3><ul>{recommendations.map((item, index) => <li key={index}>{typeof item === 'string' ? item : text(rec(item)?.title || rec(item)?.action || JSON.stringify(item))}</li>)}</ul></>}
        <h3>Approvals & remediation</h3>
        {approvals.length ? approvals.map(item => <div className="idet-approval" key={String(item.approval_id)}>
          <Badge v={String(item.status).toLowerCase()}>{String(item.status)}</Badge> <b>{text(rec(item.proposed_action)?.action, 'action')}</b>
          <p className="tiny muted">{text(rec(item.proposed_action)?.resource)} · requested {fmt(String(item.requested_at))} by {text(item.requested_by)}</p>
        </div>) : <p className="muted">No approval has been requested for this incident yet. Start an investigation in the Console to get remediation suggestions.</p>}
        {diagnosis && <details className="idet-raw"><summary>Full recorded diagnosis</summary><pre className="json">{JSON.stringify(diagnosis, null, 2)}</pre></details>}
      </Card>

      <Card icon={<ScrollText size={16} />} title="Audit timeline" wide
        actions={timeline.status === 'ready' ? <button className="btn sm" onClick={() => setReload(n => n + 1)}>Refresh</button> : undefined}>
        {timeline.status === 'loading' && <p className="muted" role="status">Loading audit timeline…</p>}
        {timeline.status === 'error' && <><p className="err" role="alert">{timeline.error}</p><button className="btn sm" onClick={() => setReload(n => n + 1)}>Retry</button></>}
        {timeline.status === 'ready' && <AnimatedTimeline events={eventsFromAudit(timeline.events)} empty="The backend returned no audit events for this incident." />}
      </Card>
    </div>
  </Page>;
}