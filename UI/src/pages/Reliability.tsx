import { useEffect, useState } from 'react';
import { Page, Kpi, DataTable, ResourceMessage, Empty, Badge } from '../ui';
import { refresh, useStore, toast } from '../store';
import { formatDuration, fmt, SERVICES } from '../data';
import { apiBlob, getCostUsage } from '../api';
import { Bars, Donut, SEVERITY_COLORS } from '../charts';
import { BackButton } from '../components';
import '../polish.css';

type Row = Record<string, unknown>;
const num = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : null);
const money = (value: number | null) => (value === null ? 'Not provided' : new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', maximumFractionDigits: 2,
}).format(value));
const ASSUMED_DAYS_PER_MONTH = 30;
type Telemetry = {
  status: 'loading' | 'ready' | 'error';
  observedAt: string;
  services: Record<string, Row | null>;
  pricing: Row;
  summary: Row;
  error: string | null;
};
const moneyUsd = (value: unknown, maximumFractionDigits = 6) => {
  const amount = num(value);
  return amount === null ? 'Not available' : new Intl.NumberFormat('en-US', {
    style: 'currency', currency: 'USD', maximumFractionDigits,
  }).format(amount);
};
export default function Reliability() {
  const state = useStore();
  const [downloading, setDownloading] = useState(false);
  const [telemetry, setTelemetry] = useState<Telemetry>({
    status: 'loading', observedAt: '', services: {}, pricing: {}, summary: {}, error: null,
  });
  const loadTelemetry = () => getCostUsage().then(result => {
    setTelemetry({
      status: 'ready',
      observedAt: typeof result.observed_at === 'string' ? result.observed_at : '',
      services: result.services as Record<string, Row | null>,
      pricing: result.pricing as Row,
      summary: result.summary as Row,
      error: null,
    });
  }).catch(reason => {
    setTelemetry(previous => ({
      ...previous,
      status: 'error',
      error: reason instanceof Error ? reason.message : 'Prometheus telemetry request failed.',
    }));
  });
  useEffect(() => {
    void loadTelemetry();
    const timer = window.setInterval(() => {
      void loadTelemetry();
    }, 30_000);
    return () => window.clearInterval(timer);
  }, []);
  const metrics = state.metrics.data || {};
  const cost = (state.cost.data || {}) as Row;
  const summary = (cost.summary && typeof cost.summary === 'object' ? cost.summary : {}) as Row;
  const period = (cost.reporting_period && typeof cost.reporting_period === 'object' ? cost.reporting_period : {}) as Record<string, string>;
  const pricing = (cost.pricing && typeof cost.pricing === 'object' ? cost.pricing : {}) as Row;
  const rows = (Array.isArray(cost.recommendations) ? cost.recommendations : []) as Row[];
  const latestIncidents = state.incidents.data || [];
  const activeServiceIncidents = latestIncidents.filter(item =>
    SERVICES.includes(String(item.service) as typeof SERVICES[number]) &&
    ['OPEN', 'REMEDIATING'].includes(String(item.status).toUpperCase()));
  const currentUsage = telemetry.status === 'ready'
    ? SERVICES.flatMap(service => {
      const usage = telemetry.services[service];
      if (!usage) return [];
      const cpu = num(usage.cpu_cores_used);
      const memory = num(usage.memory_bytes);
      return cpu === null && memory === null ? [] : [{ service, cpu, memory }];
    })
    : [];
  const highestCpuUsage = currentUsage
    .filter((item): item is typeof item & { cpu: number } => item.cpu !== null)
    .sort((a, b) => b.cpu - a.cpu)[0];
  const highestMemoryUsage = currentUsage
    .filter((item): item is typeof item & { memory: number } => item.memory !== null)
    .sort((a, b) => b.memory - a.memory)[0];
  const illustrativeCut = telemetry.status === 'ready' && telemetry.pricing.configured
    ? currentUsage.flatMap(item => {
      const usage = telemetry.services[item.service];
      if (!usage) return [];
      const cpuMonthly = num(usage.estimated_cpu_monthly_cost_usd);
      const memoryMonthly = num(usage.estimated_memory_monthly_cost_usd);
      return [
        cpuMonthly === null ? null : {
          service: item.service, component: 'CPU', monthlyCost: cpuMonthly,
          saving: num(usage.illustrative_10_percent_cpu_reduction_monthly_savings_usd),
        },
        memoryMonthly === null ? null : {
          service: item.service, component: 'memory', monthlyCost: memoryMonthly,
          saving: num(usage.illustrative_10_percent_memory_reduction_monthly_savings_usd),
        },
      ].filter((entry): entry is NonNullable<typeof entry> => entry !== null);
    }).sort((a, b) => b.monthlyCost - a.monthlyCost)[0]
    : undefined;
  const mib = (bytes: number) => (bytes / (1024 ** 2)).toFixed(1);
  const cpuRanking = currentUsage.filter((item): item is typeof item & { cpu: number } => item.cpu !== null)
    .sort((a, b) => b.cpu - a.cpu);
  const memoryRanking = currentUsage.filter((item): item is typeof item & { memory: number } => item.memory !== null)
    .sort((a, b) => b.memory - a.memory);
  const costLines = telemetry.status === 'ready' && telemetry.pricing.configured
    ? currentUsage.flatMap(item => {
      const usage = telemetry.services[item.service];
      if (!usage) return [];
      const lines: { key: string; service: string; component: string; usage: string; monthly: number }[] = [];
      const cpuMonthly = num(usage.estimated_cpu_monthly_cost_usd);
      const memoryMonthly = num(usage.estimated_memory_monthly_cost_usd);
      if (cpuMonthly !== null && item.cpu !== null) lines.push({ key: `${item.service}-cpu`, service: item.service, component: 'CPU', usage: `${item.cpu.toFixed(3)} cores`, monthly: cpuMonthly });
      if (memoryMonthly !== null && item.memory !== null) lines.push({ key: `${item.service}-mem`, service: item.service, component: 'memory', usage: `${mib(item.memory)} MiB`, monthly: memoryMonthly });
      return lines;
    }).sort((a, b) => b.monthly - a.monthly)
    : [];
  const costTotal = costLines.reduce((sum, line) => sum + line.monthly, 0);
  const reportIsNotLive = pricing.is_example === true || pricing.currency !== 'USD' ||
    /synthetic|example/i.test(String(cost.provenance || ''));
  const bySeverity = (metrics.by_severity && typeof metrics.by_severity === 'object' ? metrics.by_severity : {}) as Record<string, number>;
  const byStatus = (metrics.by_status && typeof metrics.by_status === 'object' ? metrics.by_status : {}) as Record<string, number>;
  const ok = (resource: { status: string }, value: string) =>
    resource.status === 'ready' ? value : resource.status === 'loading' ? 'Loading…' : 'Unavailable';

  // Documented calculations (derived only from rows that carry a monthly cost):
  const priced = reportIsNotLive ? [] : rows.filter(row => num(row.current_monthly_cost) !== null);
  const monthlySpend = priced.reduce((sum, row) => sum + (num(row.current_monthly_cost) ?? 0), 0);
  const weeklySpend = monthlySpend * 7 / ASSUMED_DAYS_PER_MONTH;
  const potentialMonthly = reportIsNotLive ? null : num(summary.potential_monthly_savings);
  const cuttable = reportIsNotLive ? [] : rows.filter(row => (num(row.potential_monthly_savings) ?? 0) > 0);
  const attention = reportIsNotLive ? [] : rows.filter(row => ['capacity_pressure', 'human_review_required'].includes(String(row.status)));
  const incomplete = reportIsNotLive ? [] : rows.filter(row => ['incomplete_period', 'insufficient_data'].includes(String(row.status)));

  const savingsChart = (reportIsNotLive ? [] : rows).map(row => ({
    name: String(row.resource),
    current: num(row.current_monthly_cost),
    recommended: num(row.recommended_monthly_cost),
    savings: num(row.potential_monthly_savings) ?? 0,
  }));
  const utilChart = (reportIsNotLive ? [] : rows).map(row => ({
    name: String(row.resource),
    cpu: num(row.cpu_avg) === null ? null : Number((num(row.cpu_avg)! * 100).toFixed(1)),
    memory: num(row.memory_avg) === null ? null : Number((num(row.memory_avg)! * 100).toFixed(1)),
  }));

  // Direct download: the backend renders the formatted PDF (cost tables and usage only,
  // no incident totals or incident charts) and the browser saves it straight away.
  const download = async () => {
    setDownloading(true);
    try {
      const blob = await apiBlob('/cost/report.pdf');
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url; anchor.download = 'kybernaut-live-cost-report.pdf'; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 2000);
      toast('PDF report downloaded.');
    } catch (reason) {
      toast(reason instanceof Error ? reason.message : 'The PDF download failed.');
    } finally { setDownloading(false); }
  };

  return <Page title="Reliability & Cost" sub="Current incident status and resource usage from Prometheus. USD estimates use illustrative owner-configured internal CPU and memory rates, not actual cloud-provider costs." actions={<>
    <BackButton />
    <button className="btn" onClick={() => { void refresh('metrics'); void refresh('cost'); void loadTelemetry(); }}>Refresh</button>
    <button className="btn primary" onClick={() => void download()} disabled={downloading || telemetry.status !== 'ready'}>{downloading ? 'Preparing PDF…' : 'Download report (PDF)'}</button></>}>
    <div className="print-only report-head"><b>Kybernaut · Reliability &amp; Cost report</b><span>Generated {new Date().toLocaleString()}</span></div>
    <div className="grid g4 stagger print-hide">
      <Kpi label="Total persisted incidents" value={ok(state.metrics, String(metrics.total_incidents ?? 0))} />
      <Kpi label="Active incidents · four services" value={state.incidents.status === 'ready'
        ? String(activeServiceIncidents.length) : state.incidents.status === 'loading' ? 'Loading…' : 'Unavailable'} />
      <Kpi label="Mean time to resolve" value={state.metrics.status === 'ready' ? formatDuration(metrics.mttr_seconds as number | null) : ok(state.metrics, '')} />
      <Kpi label="Recurring fingerprint rate" value={ok(state.metrics, `${(Number(metrics.recurrence_rate || 0) * 100).toFixed(1)}%`)} />
    </div>
    <p className="tiny muted print-hide">
      Total and reliability metrics include operational history from all sources. The backend reports {ok(state.metrics, String(metrics.active_incidents ?? 0))} active incidents across all sources; the four-service count above is computed from the latest incident records for order, payment, user, and inventory.
    </p>
    <div className="grid g2 stagger print-full">
      <div className="panel print-hide"><h3>Incidents by status</h3>{state.metrics.status !== 'ready'
        ? <ResourceMessage resource={state.metrics} retry={() => void refresh('metrics')} />
        : <Donut data={Object.entries(byStatus).map(([name, value]) => ({ name, value }))} colors={{ OPEN: '#DC2626', REMEDIATING: '#D97706', RESOLVED: '#16A34A' }} height={210} />}</div>
      <div className="panel print-hide"><h3>Incidents by severity</h3>{state.metrics.status !== 'ready'
        ? <ResourceMessage resource={state.metrics} retry={() => void refresh('metrics')} />
        : <Donut data={Object.entries(bySeverity).map(([name, value]) => ({ name, value }))} colors={SEVERITY_COLORS} height={210} />}</div>
    </div>

    <div className="panel">
      <div className="row between gap">
        <div><h3>Live service usage and estimated cost</h3>
          <p className="tiny muted">{telemetry.observedAt ? `Observed ${fmt(telemetry.observedAt)}` : 'A current sample is required; no missing metric is treated as zero.'}</p></div>
        <Badge v={telemetry.status === 'ready' ? 'healthy' : telemetry.status === 'error' ? 'blocked' : 'pending'}>
          {telemetry.status === 'ready' ? 'live usage' : telemetry.status === 'error' ? 'unavailable' : 'loading'}
        </Badge>
      </div>
      {telemetry.status === 'loading' && <p className="muted" role="status">Loading current Prometheus usage…</p>}
      {telemetry.status === 'error' && <><p className="err" role="alert">{telemetry.error}</p><button className="btn sm" onClick={() => void loadTelemetry()}>Retry usage</button></>}
      {telemetry.status === 'ready' && !telemetry.pricing.configured &&
        <p className="tiny muted" role="status">
          Prometheus usage is available, but USD pricing is not configured. Set KYBERNAUT_COST_CPU_PER_CORE_HOUR_USD and KYBERNAUT_COST_MEMORY_PER_GIB_HOUR_USD to illustrative owner-configured internal rates; monetary estimates remain unavailable until both are set.
        </p>}
      {telemetry.status === 'ready' && telemetry.pricing.configured &&
        <p className="tiny muted">
          Pricing: {moneyUsd(telemetry.pricing.cpu_per_core_hour_usd)} per CPU core-hour + {moneyUsd(telemetry.pricing.memory_per_gib_hour_usd)} per GiB-hour · {String(telemetry.pricing.source || 'illustrative owner-configured internal rates; not actual cloud-provider costs')}.
        </p>}
      {telemetry.status === 'ready' && telemetry.pricing.configured &&
        <div className="grid g2 stagger">
          <Kpi label="Current hourly run rate" value={moneyUsd(telemetry.summary.estimated_hourly_cost_usd)} />
          <Kpi label="Monthly run rate · 730 hours" value={moneyUsd(telemetry.summary.estimated_monthly_run_rate_usd)} />
        </div>}
      {telemetry.status === 'ready' && telemetry.pricing.configured && telemetry.summary.all_services_available === false &&
        <p className="tiny muted" role="status">Some Prometheus usage is unavailable. Per-service estimates may be shown, but the four-service total is withheld.</p>}
      {telemetry.status === 'ready' && <DataTable rows={SERVICES.map(service => ({
        service, ...(telemetry.services[service] || {}),
      }))} empty="Prometheus returned no service usage." cols={[
        { k: 'service', h: 'Service' },
        { k: 'cpu_cores_used', h: 'CPU cores', r: (row: Row) => num(row.cpu_cores_used)?.toFixed(3) ?? 'Unavailable' },
        { k: 'memory_bytes', h: 'Memory (MiB)', r: (row: Row) => {
          const value = num(row.memory_bytes);
          return value === null ? 'Unavailable' : (value / (1024 ** 2)).toFixed(1);
        } },
        { k: 'request_rate', h: 'Requests/sec', r: (row: Row) => num(row.request_rate)?.toFixed(2) ?? 'Unavailable' },
        { k: 'p95_latency_seconds', h: 'p95 latency', r: (row: Row) => {
          const value = num(row.p95_latency_seconds);
          return value === null ? 'Unavailable' : `${(value * 1000).toFixed(1)} ms`;
        } },
        { k: 'estimated_hourly_cost_usd', h: 'Estimated /hour', r: (row: Row) => moneyUsd(row.estimated_hourly_cost_usd) },
        { k: 'estimated_monthly_run_rate_usd', h: 'Estimated /month', r: (row: Row) => moneyUsd(row.estimated_monthly_run_rate_usd) },
      ]} />}
      {telemetry.status === 'ready' && <p className="tiny muted">{String(telemetry.summary.month_hours || 730)}-hour extrapolation from current Prometheus process usage and illustrative owner-configured internal rates. Not actual cloud-provider costs or an invoice; excludes host overhead and other workloads.</p>}
    </div>

    {state.cost.status !== 'ready' || reportIsNotLive
      ? <div className="panel">
        <h3>Live Prometheus cost estimate</h3>
        <p className="muted">
          The figures below use current CPU and memory measurements from Prometheus with illustrative owner-configured USD rates. The monthly value is a 730-hour run-rate estimate, not a weekly bill or actual cloud-provider cost.
        </p>
        {telemetry.status === 'ready' && telemetry.pricing.configured
          ? <div className="grid g2 stagger">
            <Kpi label="Current hourly run rate" value={moneyUsd(telemetry.summary.estimated_hourly_cost_usd)} />
            <Kpi label="Monthly run rate · 730 hours" value={moneyUsd(telemetry.summary.estimated_monthly_run_rate_usd)} />
          </div>
          : <p className="tiny muted" role="status">
            Live monetary estimates are unavailable until Prometheus usage and approved pricing are both available.
          </p>}
        <h4 className="pl-h4">Where can we cut?</h4>
        {telemetry.status === 'ready' && currentUsage.length > 0
          ? <>
            <div className="pl-cut-grid">
              <div className="pl-cut-card">
                <b>Highest current CPU usage</b>
                <p className="pl-cut-head">{highestCpuUsage
                  ? `${highestCpuUsage.service} · ${highestCpuUsage.cpu.toFixed(3)} cores`
                  : 'Unavailable from the current Prometheus sample.'}</p>
                <ol className="pl-rank-mini" aria-label="CPU usage ranking">{cpuRanking.map(item => <li key={item.service}>
                  <span>{item.service}</span><i style={{ width: `${Math.max(4, (item.cpu / (cpuRanking[0].cpu || 1)) * 100)}%` }} /><em>{item.cpu.toFixed(3)} cores</em></li>)}</ol>
              </div>
              <div className="pl-cut-card">
                <b>Highest current memory usage</b>
                <p className="pl-cut-head">{highestMemoryUsage
                  ? `${highestMemoryUsage.service} · ${mib(highestMemoryUsage.memory)} MiB`
                  : 'Unavailable from the current Prometheus sample.'}</p>
                <ol className="pl-rank-mini" aria-label="Memory usage ranking">{memoryRanking.map(item => <li key={item.service}>
                  <span>{item.service}</span><i style={{ width: `${Math.max(4, (item.memory / (memoryRanking[0].memory || 1)) * 100)}%` }} /><em>{mib(item.memory)} MiB</em></li>)}</ol>
              </div>
              {illustrativeCut && illustrativeCut.saving !== null
                ? <div className="pl-cut-card pl-cut-cost">
                  <b>Largest current cost component: {illustrativeCut.service} {illustrativeCut.component}</b>
                  <p className="tiny">Estimated at {moneyUsd(illustrativeCut.monthlyCost)}/month. If its current measured usage were 10% lower, the approximate run-rate reduction would be {moneyUsd(illustrativeCut.saving)}/month.</p>
                  <p className="tiny muted">Illustrative what-if only—not a recommendation or guaranteed saving. Validate historical peaks, requests/limits, and service impact before changing capacity.</p>
                </div>
                : <div className="pl-cut-card"><b>Cost components</b><p className="tiny muted">Per-service cost and hypothetical savings are unavailable until current usage and both USD rates are available.</p></div>}
            </div>
            {costLines.length > 0 && <div className="pl-rank" aria-label="Cost line items ranked">
              <div className="pl-rank-head"><b>Review these first</b><span className="tiny muted">Ranked by monthly cost · what-if reductions are hypothetical</span></div>
              <div className="pl-rank-row pl-rank-cols" aria-hidden="true"><span>#</span><span>Line item</span><span>Share of run rate</span><span>Per month</span><span>If −10%</span><span>If −30%</span></div>
              {costLines.slice(0, 6).map((line, index) => <div className="pl-rank-row" key={line.key}>
                <span className="pl-rank-n">{index + 1}</span>
                <span className="pl-rank-name"><b>{line.service} {line.component}</b><small className="muted">{line.usage}</small></span>
                <span className="pl-rank-bar"><i style={{ width: `${costTotal ? Math.max(3, (line.monthly / costTotal) * 100) : 0}%` }} /><em>{costTotal ? ((line.monthly / costTotal) * 100).toFixed(1) : '0.0'}%</em></span>
                <span className="pl-rank-money">{moneyUsd(line.monthly, 2)}</span>
                <span className="pl-rank-money save">−{moneyUsd(line.monthly * 0.1, 2)}</span>
                <span className="pl-rank-money save">−{moneyUsd(line.monthly * 0.3, 2)}</span>
              </div>)}
            </div>}
            <div className="pl-check">
              <b>Before you cut anything</b>
              <ul>
                <li>This is one point-in-time sample: confirm peak and p95 usage over at least 7 days.</li>
                <li>Compare with configured CPU/memory requests and limits; usage near a limit means the service is already tight.</li>
                <li>Check request rate, p95 latency and open incidents for the service first.</li>
                <li>Any capacity change needs a human decision through the normal approval workflow.</li>
              </ul>
            </div>
            <p className="tiny muted">Current usage signals identify workloads to review; they are not savings recommendations. Safe right-sizing requires historical peaks and configured CPU/memory requests or limits.</p>
          </>
          : <p className="tiny muted">Current Prometheus usage is unavailable, so no workload is ranked for review.</p>}
        {state.cost.status !== 'ready' && <ResourceMessage resource={state.cost} retry={() => void refresh('cost')} />}
      </div>
      : <>
        {!reportIsNotLive && <div className="panel costhead">
          <div><h3>Weekly cost estimate</h3>
            <p className="muted">Week {fmt(period.start)} – {fmt(period.end_exclusive)} · generated {fmt(cost.generated_at as string)}</p>
            <p className="tiny muted">Data source: {String(cost.provenance || 'not stated')} · Pricing: {String(pricing.source || 'not stated')}</p></div>
          {pricing.is_example === true && <Badge v="pending">Example pricing</Badge>}
        </div>}
        {!reportIsNotLive && <>
        <div className="grid g4 stagger">
          <Kpi label="Estimated spend this week" value={priced.length ? money(weeklySpend) : 'Not provided'} />
          <Kpi label="Estimated monthly spend" value={priced.length ? money(monthlySpend) : 'Not provided'} />
          <Kpi label="Can be cut (monthly, estimate)" value={money(potentialMonthly)} />
          <Kpi label="Realized savings" value={money(num(summary.realized_savings))} />
        </div>
        <p className="tiny muted">Weekly spend = monthly cost × 7 ÷ {ASSUMED_DAYS_PER_MONTH}, using only the {priced.length} of {rows.length} resources that carry a monthly cost. Estimated annual savings: {money(num(summary.estimated_annual_savings))}. Savings are estimates until an approved change is applied and measured; nothing here changes infrastructure.</p>

        <div className="grid g2 stagger">
          <div className="panel"><h3>Monthly cost: current vs recommended</h3>
            <Bars data={savingsChart} keys={[{ key: 'current', label: 'Current', color: '#94A3B8' }, { key: 'recommended', label: 'Recommended', color: '#0E8C8F' }, { key: 'savings', label: 'Potential saving', color: '#16A34A' }]} height={240} />
            <p className="tiny muted">Resources without a priced recommendation are left blank.</p></div>
          <div className="panel"><h3>Average utilization this week (%)</h3>
            <Bars data={utilChart} keys={[{ key: 'cpu', label: 'CPU %', color: '#7CD1CB' }, { key: 'memory', label: 'Memory %', color: '#D97706' }]} unit="%" height={240} /></div>
        </div>

        <div className="grid g3 stagger">
          <div className="panel"><h3>Where we can cut</h3>
            {cuttable.length ? cuttable.map(row => <div className="qa blk" key={String(row.resource)}>
              <b>{String(row.resource)}</b> <Badge v="healthy">save {money(num(row.potential_monthly_savings))}/mo</Badge>
              <p className="tiny muted">{String(row.current_type || '—')} → {String(row.recommended_type || 'review')} · {money(num(row.current_monthly_cost))} → {money(num(row.recommended_monthly_cost))}</p>
              <p className="tiny">{String(row.rationale || '')}</p>
              {row.suggested_next_step ? <p className="tiny muted">Next: {String(row.suggested_next_step)}</p> : null}
            </div>) : <Empty text="No downsize candidates this week." />}</div>
          <div className="panel"><h3>Needs attention</h3>
            {attention.length ? attention.map(row => <div className="qa blk" key={String(row.resource)}>
              <b>{String(row.resource)}</b> <Badge v="pending">{String(row.status).replace(/_/g, ' ')}</Badge>
              <p className="tiny">{String(row.rationale || '')}</p></div>) : <Empty text="No resources need attention." />}</div>
          <div className="panel"><h3>Incomplete data</h3>
            {incomplete.length ? incomplete.map(row => <div className="qa blk" key={String(row.resource)}>
              <b>{String(row.resource)}</b> <Badge v="blocked">{String(row.status).replace(/_/g, ' ')}</Badge>
              <p className="tiny muted">{String(row.sample_count ?? '?')} of {String(row.expected_samples ?? '?')} samples</p></div>) : <Empty text="All resources have complete data." />}</div>
        </div>

        <DataTable rows={rows} empty="The report contains no recommendation rows." cols={[
          { k: 'resource', h: 'Resource' }, { k: 'status', h: 'Status', r: (row: any) => String(row.status).replace(/_/g, ' ') },
          { k: 'current_type', h: 'Type' },
          { k: 'cpu_avg', h: 'CPU avg', r: (row: any) => row.cpu_avg == null ? '—' : `${(row.cpu_avg * 100).toFixed(1)}%` },
          { k: 'memory_avg', h: 'Memory avg', r: (row: any) => row.memory_avg == null ? '—' : `${(row.memory_avg * 100).toFixed(1)}%` },
          { k: 'current_monthly_cost', h: 'Current /mo', r: (row: any) => row.current_monthly_cost ?? '—' },
          { k: 'recommended_monthly_cost', h: 'Recommended /mo', r: (row: any) => row.recommended_monthly_cost ?? '—' },
          { k: 'potential_monthly_savings', h: 'Potential saving /mo', r: (row: any) => row.potential_monthly_savings ?? '—' },
          { k: 'rationale', h: 'Rationale' },
        ]} />
        {cost.disclaimer ? <p className="muted">{String(cost.disclaimer)}</p> : null}
        </>}
      </>}
  </Page>;
}