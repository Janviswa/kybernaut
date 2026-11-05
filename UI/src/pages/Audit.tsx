import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Page, DataTable, Modal, ResourceMessage, Tabs } from '../ui';
import { refresh, useStore } from '../store';
import { fmt } from '../data';
import { AuditTree, BackButton } from '../components';

const csvCell = (value: unknown) => `"${String(value ?? '').replace(/"/g, '""')}"`;

export default function Audit() {
  const state = useStore();
  const [params] = useSearchParams();
  const [query, setQuery] = useState(params.get('q') || '');
  const [view, setView] = useState('tree');
  const [selected, setSelected] = useState<Record<string, unknown> | null>(null);
  const events = state.audit.data || [];
  const rows = events.filter(event =>
    `${event.actor || ''} ${event.event_type || ''} ${event.resource || ''} ${event.outcome || ''} ${event.incident_ref || ''}`
      .toLowerCase().includes(query.toLowerCase()));
  const exportCsv = () => {
    const fields = ['id', 'timestamp', 'actor', 'event_type', 'resource', 'incident_ref', 'outcome'];
    const body = [fields.map(csvCell).join(','), ...rows.map(event => fields.map(field => csvCell(event[field])).join(','))].join('\n');
    const url = URL.createObjectURL(new Blob([body], { type: 'text/csv' }));
    const anchor = document.createElement('a');
    anchor.href = url; anchor.download = 'kybernaut-audit-events.csv'; anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return <Page title="Audit Trail" sub="Audit records returned by the backend, grouped by incident as an expandable timeline tree." actions={<>
    <BackButton />
    <button className="btn" onClick={exportCsv} disabled={state.audit.status !== 'ready'}>Export CSV</button>
    <button className="btn" onClick={() => void refresh('audit')}>Refresh</button></>}>
    <div className="row gap wrap">
      <input aria-label="Search audit" placeholder="Search actor, event, resource, incident" value={query} onChange={event => setQuery(event.target.value)} style={{ minWidth: 280 }} />
      <Tabs tabs={[['tree', 'Timeline tree'], ['table', 'Table']]} v={view} set={setView} />
    </div>
    {state.audit.status !== 'ready'
      ? <ResourceMessage resource={state.audit} retry={() => void refresh('audit')} />
      : view === 'tree'
        ? <div className="panel treepanel"><AuditTree rows={rows} onSelect={setSelected} /></div>
        : <DataTable rows={rows} onRow={(event: any) => setSelected(event)} empty="No audit events returned." cols={[
          { k: 'timestamp', h: 'Timestamp', r: (event: any) => fmt(event.timestamp) },
          { k: 'actor', h: 'Actor' }, { k: 'event_type', h: 'Event' },
          { k: 'resource', h: 'Resource' }, { k: 'incident_ref', h: 'Incident' },
          { k: 'outcome', h: 'Outcome' },
        ]} />}
    {selected && <Modal title={String(selected.event_type || 'Audit event')} onClose={() => setSelected(null)}>
      <dl className="kv">{Object.entries(selected).map(([key, value]) => [
        <dt key={key}>{key}</dt>,
        <dd key={`${key}-value`}>{typeof value === 'object' ? JSON.stringify(value) : String(value ?? '—')}</dd>,
      ])}</dl>
    </Modal>}
  </Page>;
}