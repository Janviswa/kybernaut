import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { Page, DataTable, Badge, Empty, ResourceMessage, Kpi } from '../ui';
import { refresh, useStore } from '../store';
import { SERVICES, fmt } from '../data';
import { effectiveServiceStatus } from '../openIncidents';
import { getLiveTelemetry } from '../api';
import { Bars } from '../charts';
import { AnimatedTimeline, BackButton, Scrollbox } from '../components';

type Row = Record<string, unknown>;
const rec = (value: unknown): Row => (value && typeof value === 'object' && !Array.isArray(value) ? value as Row : {});
const num = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : null);
const show = (value: number | null, digits = 4, suffix = '') => (value === null ? 'Unavailable' : `${Number(value.toFixed(digits))}${suffix}`);
const mb = (bytes: number | null) => (bytes === null ? null : bytes / 1024 / 1024);
const activeIncident = (item: Row) => ['OPEN', 'REMEDIATING'].includes(String(item.status));
const localSimulation = (item: Row) =>
  rec(item.source_evidence).simulation === 'local_service_failure';
const incidentMessage = (item: Row) => String(item.message || item.incident_type || 'Active incident')
  .replace(/^LOCAL SIMULATION:\s*/i, '')
  .replace(/\bthe local (order|payment|user|inventory) service\b/gi, 'the $1 service')
  .replace(/\bsimulated fault\b/gi, 'service fault')
  .replace(/\bsimulated\b|\bsimulator\b/gi, '')
  .replace(/\bsimulation\b/gi, 'service');

function useServiceRefresh() {
  useEffect(() => {
    const load = () => {
      void refresh('services');
      void refresh('incidents');
    };
    load();
    const timer = window.setInterval(load, 15_000);
    return () => window.clearInterval(timer);
  }, []);
}

/** Live Prometheus telemetry per service; refreshed every 15 s. */
function useTelemetry() {
  const [state, setState] = useState<{ status: 'loading' | 'ready' | 'error'; services: Row; unavailable: string[]; observedAt: string; error: string }>(
    { status: 'loading', services: {}, unavailable: [], observedAt: '', error: '' });
  useEffect(() => {
    let active = true;
    const load = () => getLiveTelemetry().then(result => {
      if (active) setState({ status: 'ready', services: result.services, unavailable: result.unavailable_services, observedAt: result.observed_at, error: '' });
    }).catch(reason => {
      if (active) setState(previous => ({ ...previous, status: 'error', error: reason instanceof Error ? reason.message : 'Telemetry request failed.' }));
    });
    void load();
    const timer = window.setInterval(() => void load(), 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  return state;
}

export function Services() {
  useServiceRefresh();
  const state = useStore();
  const nav = useNavigate();
  const telemetry = useTelemetry();
  const [query, setQuery] = useState('');
  const [health, setHealth] = useState('all');
  const records = state.services.data || [];
  const incidents = state.incidents.data || [];
  const activeFor = (service: string) => incidents.filter(item =>
    item.service === service && activeIncident(item)).length;
  const rows = records.map(record => {
      const simulation = incidents.find(item =>
        item.service === record.service && activeIncident(item) && localSimulation(item));
      const scenario = simulation ? String(simulation.incident_type) : '';
      void scenario;
      const effectiveStatus = effectiveServiceStatus(record.service, record.data?.status, incidents);
      const evidence = rec(record.data?.evidence);
      const features = rec(telemetry.services[record.service]);
      return {
        ...record,
        simulation,
        effectiveStatus,
        p95: num(evidence.p95_latency_seconds) ?? num(features.p95_latency_seconds),
        e5xx: num(evidence.http_5xx_rate_per_second),
        cpu: num(features.cpu_cores_used), mem: mb(num(features.memory_bytes)), rps: num(features.request_rate),
        active: activeFor(record.service),
      };
    }).filter(row =>
      row.service.toLowerCase().includes(query.toLowerCase()) &&
      (health === 'all' || (row.status === 'ready' && row.effectiveStatus === health)));
  const chart = rows.map(row => ({
    name: row.service.replace('-service', ''),
    p95_ms: row.p95 === null ? null : Number((row.p95 * 1000).toFixed(2)),
    cpu_millicores: row.cpu === null ? null : Number((row.cpu * 1000).toFixed(1)),
    memory_mb: row.mem === null ? null : Number(row.mem.toFixed(1)),
  }));

  return <Page title="Service Health" sub="Diagnosis and live telemetry from Prometheus-backed backend responses. Unknown and unavailable are not healthy." actions={<BackButton />}>
    <div className="row gap wrap">
      <input aria-label="Search services" placeholder="Search services" value={query} onChange={event => setQuery(event.target.value)} />
      <select aria-label="Health filter" value={health} onChange={event => setHealth(event.target.value)}>
        <option value="all">All diagnosis states</option><option value="up">up</option><option value="degraded">degraded</option><option value="down">down</option><option value="unknown">unknown</option>
      </select>
      <button className="btn" onClick={() => { void refresh('services'); void refresh('incidents'); }}>Refresh</button>
      {telemetry.observedAt && <span className="tiny muted">Telemetry observed {fmt(telemetry.observedAt)}</span>}
    </div>
    {state.services.status !== 'ready'
      ? <ResourceMessage resource={state.services} retry={() => void refresh('services')} />
      : <>
        <div className="grid g3 stagger">
          <div className="panel"><h3>p95 latency (ms)</h3><Bars data={chart} keys={[{ key: 'p95_ms', label: 'p95 (ms)' }]} unit=" ms" height={190} /></div>
          <div className="panel"><h3>CPU (millicores)</h3><Bars data={chart} keys={[{ key: 'cpu_millicores', label: 'CPU', color: '#7CD1CB' }]} height={190} /></div>
          <div className="panel"><h3>Memory (MB)</h3><Bars data={chart} keys={[{ key: 'memory_mb', label: 'Memory', color: '#D97706' }]} height={190} /></div>
        </div>
        {telemetry.status === 'error' && <p className="err" role="alert">{telemetry.error} Latency, CPU and memory columns may show “Unavailable”.</p>}
        <DataTable rows={rows} onRow={(row: any) => nav(`/services/${row.service}`)} empty="No matching service diagnosis results." cols={[
          { k: 'service', h: 'Service' },
          { k: 'status', h: 'Availability', r: (row: any) => <Badge v={row.status !== 'ready' ? 'blocked' : row.effectiveStatus === 'up' ? 'healthy' : row.effectiveStatus === 'down' ? 'critical' : 'blocked'}>{row.status !== 'ready' ? 'unavailable' : row.effectiveStatus}</Badge> },
          { k: 'p95', h: 'p95 latency', r: (row: any) => show(row.p95, 4, ' s') },
          { k: 'e5xx', h: 'HTTP 5xx rate', r: (row: any) => show(row.e5xx, 4, ' /s') },
          { k: 'cpu', h: 'CPU cores', r: (row: any) => show(row.cpu, 3) },
          { k: 'mem', h: 'Memory', r: (row: any) => show(row.mem, 1, ' MB') },
          { k: 'rps', h: 'Request rate', r: (row: any) => show(row.rps, 3, ' /s') },
          { k: 'evidence', h: 'Evidence', r: (row: any) => row.status === 'ready' ? String(row.data?.evidence_status || 'not reported') : row.error },
          { k: 'missing', h: 'Missing evidence', r: (row: any) => row.status === 'ready' ? (Array.isArray(row.data?.missing_evidence) && row.data.missing_evidence.length ? row.data.missing_evidence.join(', ') : 'None reported') : '—' },
          { k: 'simulation', h: 'Active incident', r: (row: any) => row.simulation ? incidentMessage(row.simulation) : 'None' },
          { k: 'active', h: 'Active incidents' },
        ]} />
      </>}
  </Page>;
}

export function ServiceDetail() {
  useServiceRefresh();
  const { serviceId } = useParams();
  const state = useStore();
  const telemetry = useTelemetry();
  const result = state.services.data?.find(item => item.service === serviceId);
  const incidents = (state.incidents.data || []).filter(item => item.service === serviceId);
  if (!serviceId || !SERVICES.includes(serviceId as typeof SERVICES[number])) {
    return <Page title="Service not found" actions={<BackButton fallback="/services" />}><p className="muted">This service is not in the configured service list.</p></Page>;
  }
  if (state.services.status !== 'ready') {
    return <Page title={serviceId} actions={<BackButton fallback="/services" />}><ResourceMessage resource={state.services} retry={() => void refresh('services')} /></Page>;
  }
  if (!result || result.status === 'error') {
    return <Page title={serviceId} sub="Diagnosis unavailable" actions={<BackButton fallback="/services" />}>
      <ResourceMessage resource={{ status: 'error', error: result?.error || 'No diagnosis response was returned.' }} retry={() => void refresh('services')} /></Page>;
  }
  const diagnosis = result.data || {};
  const evidence = rec(diagnosis.evidence);
  const features = rec(telemetry.services[serviceId]);
  const findings = Array.isArray(diagnosis.findings) ? diagnosis.findings as Row[] : [];
  const missing = Array.isArray(diagnosis.missing_evidence) ? diagnosis.missing_evidence as unknown[] : [];
  const simulation = incidents.find(item =>
    activeIncident(item) && localSimulation(item));
  const simulatedDown = simulation?.incident_type === 'service_unavailable';
  const status = simulation
    ? effectiveServiceStatus(serviceId, undefined, incidents)
    : String(diagnosis.status || 'unknown');
  const tone = status.startsWith('up') ? 'healthy' : status.startsWith('down') ? 'critical' : 'blocked';
  const p95 = num(evidence.p95_latency_seconds) ?? num(features.p95_latency_seconds);
  const compare = (state.services.data || []).filter(item => item.status === 'ready').map(item => ({
    name: item.service.replace('-service', ''),
    p95_ms: (() => { const value = num(rec(item.data?.evidence).p95_latency_seconds); return value === null ? null : Number((value * 1000).toFixed(2)); })(),
  }));
  const timeline = incidents.slice(0, 20).map((item, index) => ({
    id: `${item.id ?? index}-${item.incident_id}`, title: `${String(item.incident_type)} · ${String(item.severity)}`,
    time: String(item.opened_at || ''), outcome: String(item.status), resource: String(item.incident_id), detail: incidentMessage(item),
  }));

  return <Page title={serviceId} sub={`Observed status: ${status}`} actions={<><BackButton fallback="/services" /><Badge v={tone}>{status}</Badge></>}>
    {simulation && <div className="panel" role="status">
      <Badge v={simulatedDown ? 'critical' : 'blocked'}>Active incident</Badge>
      <p><b>{String(simulation.incident_type).replace(/_/g, ' ')}</b> — {incidentMessage(simulation)}</p>
    </div>}
    <div className="grid g5 compact stagger">
      <Kpi label="HTTP 5xx (req/s)" value={show(num(evidence.http_5xx_rate_per_second))} />
      <Kpi label="p95 latency (s)" value={show(p95)} />
      <Kpi label="CPU cores" value={show(num(features.cpu_cores_used), 3)} />
      <Kpi label="Memory (MB)" value={show(mb(num(features.memory_bytes)), 1)} />
      <Kpi label="Active incidents" value={state.incidents.status === 'ready' ? incidents.filter(item => ['OPEN', 'REMEDIATING'].includes(String(item.status))).length : 'Unavailable'} />
    </div>
    <div className="fitgrid">
      <Scrollbox title="Diagnosis findings">
        {findings.length ? findings.map((finding, index) => <div className="qa blk" key={`${String(finding.type)}-${index}`}>
          <Badge v={String(finding.severity || 'unknown')}>{String(finding.severity || 'unknown')}</Badge> <b>{String(finding.type || 'Finding')}</b>
          <p>{String(finding.evidence || 'No finding details were returned.')}</p>
          {finding.recommendation ? <p className="muted">{String(finding.recommendation)}</p> : null}
        </div>) : <Empty text="No findings were returned." />}
      </Scrollbox>
      <Scrollbox title="Evidence & monitoring completeness">
        <dl className="kvl">
          <dt>Evidence status</dt><dd>{String(diagnosis.evidence_status || 'not reported')}</dd>
          <dt>Missing</dt><dd>{missing.length ? missing.join(', ') : 'None reported'}</dd>
          <dt>Telemetry</dt><dd>{telemetry.unavailable.includes(serviceId) ? 'Unavailable for this service' : telemetry.status === 'ready' ? `Observed ${fmt(telemetry.observedAt)}` : telemetry.status === 'error' ? telemetry.error : 'Loading…'}</dd>
          <dt>Request rate</dt><dd>{show(num(features.request_rate), 3, ' /s')}</dd>
        </dl>
        <h3>Evidence timestamps</h3><pre className="json">{JSON.stringify(diagnosis.evidence_timestamps ?? {}, null, 2)}</pre>
      </Scrollbox>
      <Scrollbox title="p95 vs other services">
        <Bars data={compare} keys={[{ key: 'p95_ms', label: 'p95 (ms)' }]} unit=" ms" height={200} />
        <p className="tiny muted">Current values only; the backend exposes no latency history.</p>
      </Scrollbox>
      <Scrollbox title="Incident timeline">
        <AnimatedTimeline events={timeline} empty={state.incidents.status === 'ready' ? 'No persisted incidents for this service.' : 'Incident records unavailable.'} />
      </Scrollbox>
    </div>
  </Page>;
}