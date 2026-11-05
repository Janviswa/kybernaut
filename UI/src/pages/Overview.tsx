import { ReactNode, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Page, Kpi, Badge, DataTable, ResourceMessage } from '../ui';
import { refresh, useStore } from '../store';
import { fmt, formatDuration, SERVICES } from '../data';
import { Bars, Donut, SEVERITY_COLORS, TrendArea } from '../charts';
import { effectiveServiceStatus, openIncidentsOf } from '../openIncidents';


const DAY = 86_400_000;

export default function Overview() {
  const state = useStore();
  const nav = useNavigate();
  const allIncidents = state.incidents.data || [];
  // Same scope as the sidebar badge, the bell and Incidents & Alerts: the four
  // application services only. Kubernetes lab history is counted separately below.
  const incidents = allIncidents.filter(item => (SERVICES as readonly string[]).includes(String(item.service)));
  const labHidden = allIncidents.length - incidents.length;
  const services = state.services.data || [];
  const approvals = state.approvals.data || [];
  const active = openIncidentsOf(allIncidents);
  const effective = (item: (typeof services)[number]) =>
    item.status === 'ready' ? effectiveServiceStatus(item.service, item.data?.status, allIncidents) : 'unknown';
  const counts = (status: string) => services.filter(item => effective(item) === status).length;
  const kpi = (resource: { status: string }, value: ReactNode) =>
    resource.status === 'ready' ? value : resource.status === 'loading' ? 'Loading…' : 'Unavailable';

  // Keep the dashboard live without a manual refresh.
  useEffect(() => {
    const timer = window.setInterval(() => {
      void Promise.all([refresh('incidents'), refresh('approvals'), refresh('services')]);
    }, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  // Real time series: incidents bucketed by the day they were opened (last 7 days).
  const trend = useMemo(() => {
    const start = new Date(); start.setHours(0, 0, 0, 0);
    const days = Array.from({ length: 7 }, (_, index) => {
      const date = new Date(start.getTime() - (6 - index) * DAY);
      return { label: date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' }), from: date.getTime(), critical: 0, high: 0, medium: 0, low: 0 };
    });
    incidents.forEach(item => {
      const opened = Date.parse(String(item.opened_at));
      const bucket = days.find(day => opened >= day.from && opened < day.from + DAY);
      const severity = String(item.severity || 'low') as 'critical' | 'high' | 'medium' | 'low';
      if (bucket && severity in bucket) bucket[severity] += 1;
    });
    return days;
  }, [incidents]);

  const severity = (['critical', 'high', 'medium', 'low'] as const).map(name => ({
    name, value: incidents.filter(item => String(item.severity) === name).length,
  }));
  const statusMix = ['OPEN', 'REMEDIATING', 'RESOLVED'].map(name => ({
    name, value: incidents.filter(item => String(item.status) === name).length,
  }));
  const latency = services.filter(item => item.status === 'ready').map(item => {
    const evidence = (item.data?.evidence || {}) as Record<string, unknown>;
    return {
      name: item.service.replace('-service', ''),
      p95_ms: typeof evidence.p95_latency_seconds === 'number' ? Number((evidence.p95_latency_seconds * 1000).toFixed(2)) : null,
    };
  });

  return <Page title="Overview" sub={`Live backend summary; only returned records and measurements are shown.${labHidden > 0 ? ` Incident figures cover the four application services; ${labHidden} Kubernetes lab history record${labHidden === 1 ? ' is' : 's are'} excluded.` : ''}`} actions={
    <button className="btn" onClick={() => {
      void Promise.all([refresh('incidents'), refresh('services'), refresh('approvals'), refresh('audit'), refresh('metrics')]);
    }}>Refresh</button>
  }>
    <div className="grid g4 stagger">
      <Kpi label="Open incidents" value={kpi(state.incidents, active.length)} onClick={() => nav('/incidents')} />
      <Kpi tone="cr" label="Critical open incidents" value={kpi(state.incidents, active.filter(item => item.severity === 'critical').length)} onClick={() => nav('/incidents')} />
      <Kpi label="Services up / degraded / down" value={state.services.status === 'ready' ? `${counts('up')} / ${counts('degraded')} / ${counts('down')}` : state.services.status === 'loading' ? 'Loading…' : 'Unavailable'} onClick={() => nav('/services')} />
      <Kpi label="Pending approvals" value={kpi(state.approvals, approvals.filter(item => item.status === 'PENDING').length)} onClick={() => nav('/incidents')} />
      <Kpi label="Resolved incidents with duration" value={state.metrics.status === 'ready' ? state.metrics.data?.resolved_incidents_with_duration ?? '—' : state.metrics.status === 'loading' ? 'Loading…' : 'Unavailable'} onClick={() => nav('/reliability-cost')} />
      <Kpi label="Mean time to resolve" value={state.metrics.status === 'ready' ? formatDuration(state.metrics.data?.mttr_seconds as number | null) : state.metrics.status === 'loading' ? 'Loading…' : 'Unavailable'} onClick={() => nav('/reliability-cost')} />
    </div>

    <div className="grid g2 stagger">
      <div className="panel"><h3>Incident trend · last 7 days</h3>
        <p className="tiny muted">Counted from the opened-at time of persisted incidents.</p>
        {state.incidents.status !== 'ready' ? <ResourceMessage resource={state.incidents} retry={() => void refresh('incidents')} />
          : <TrendArea data={trend} keys={['critical', 'high', 'medium', 'low']} />}</div>
      <div className="panel"><h3>Incidents by severity</h3>
        {state.incidents.status !== 'ready' ? <ResourceMessage resource={state.incidents} retry={() => void refresh('incidents')} />
          : <Donut data={severity} colors={SEVERITY_COLORS} />}</div>
    </div>

    <div className="grid g2 stagger">
      <div className="panel"><h3>Service p95 latency (ms)</h3>
        {state.services.status !== 'ready' ? <ResourceMessage resource={state.services} retry={() => void refresh('services')} />
          : <Bars data={latency} keys={[{ key: 'p95_ms', label: 'p95 latency (ms)' }]} unit=" ms" />}
        <p className="tiny muted">Missing measurements are left blank, never drawn as zero.</p></div>
      <div className="panel"><h3>Incident status mix</h3>
        <Donut data={statusMix} colors={{ OPEN: '#DC2626', REMEDIATING: '#D97706', RESOLVED: '#16A34A' }} />
        <div className="col" style={{ marginTop: 6 }}>{services.map(item => <div className="row between" key={item.service}>
          <span>{item.service}</span><Badge v={item.status === 'ready' ? (effective(item) === 'up' ? 'healthy' : effective(item) === 'down' ? 'critical' : 'pending') : 'blocked'}>
            {item.status === 'ready' ? effective(item) : 'unavailable'}</Badge></div>)}</div></div>
    </div>

    {state.metrics.status === 'error' && <ResourceMessage resource={state.metrics} retry={() => void refresh('metrics')} />}
    <div className="grid g2 stagger">
      <div><h3>Recent incidents</h3>
        {state.incidents.status !== 'ready'
          ? <ResourceMessage resource={state.incidents} retry={() => void refresh('incidents')} />
          : <DataTable size={5} onRow={(row: any) => nav(`/incidents/${row.incident_id}`)} rows={incidents.slice(0, 5)} empty="No persisted incidents." cols={[
            { k: 'incident_id', h: 'ID' }, { k: 'incident_type', h: 'Type' },
            { k: 'severity', h: 'Severity', r: (row: any) => <Badge v={String(row.severity || 'unknown')} /> },
            { k: 'status', h: 'Status', r: (row: any) => <Badge v={String(row.status || 'unknown').toLowerCase()} /> },
          ]} />}
      </div>
      <div><h3>Recent audit events</h3>
        {state.audit.status !== 'ready'
          ? <ResourceMessage resource={state.audit} retry={() => void refresh('audit')} />
          : <DataTable size={5} rows={state.audit.data || []} onRow={(row: any) => nav(`/audit?q=${encodeURIComponent(String(row.incident_ref || row.resource || ''))}`)} empty="No audit events returned." cols={[
            { k: 'event_type', h: 'Event' }, { k: 'actor', h: 'Actor' },
            { k: 'timestamp', h: 'Time', r: (row: any) => fmt(row.timestamp) },
          ]} />}
      </div>
    </div>
  </Page>;
}