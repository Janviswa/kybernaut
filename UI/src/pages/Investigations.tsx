import { useEffect, useMemo, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { Eye, MessageSquare, Search, Trash2 } from 'lucide-react';
import { Page, DataTable, Badge, ResourceMessage, Confirm } from '../ui';
import { InvestigationSummary, deleteInvestigation, listInvestigations } from '../api';
import { fmt } from '../data';
import { BackButton } from '../components';
import { toast, useStore } from '../store';
import '../polish.css';

type Filter = 'all' | 'open' | 'resolved' | 'none';

const isOpen = (row: InvestigationSummary) =>
  !!row.incident_id && String(row.incident_status || '').toUpperCase() !== 'RESOLVED';

export function Investigations() {
  const nav = useNavigate();
  const { operatorId, permissions } = useStore();
  const [state, setState] = useState<{ status: 'loading' | 'ready' | 'error'; rows: InvestigationSummary[]; error: string | null }>({ status: 'loading', rows: [], error: null });
  const [reload, setReload] = useState(0);
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<Filter>('all');
  const [pendingDelete, setPendingDelete] = useState<InvestigationSummary | null>(null);

  useEffect(() => {
    let active = true;
    setState(previous => ({ ...previous, status: previous.rows.length ? 'ready' : 'loading' }));
    listInvestigations().then(rows => { if (active) setState({ status: 'ready', rows, error: null }); })
      .catch(reason => { if (active) setState({ status: 'error', rows: [], error: reason instanceof Error ? reason.message : 'Could not load saved investigations.' }); });
    return () => { active = false; };
  }, [reload]);

  const openRow = (row: InvestigationSummary) => nav(`/console?investigation=${encodeURIComponent(row.investigation_id)}`);
  const canDelete = (row: InvestigationSummary) => row.operator_id === operatorId || permissions.includes('owner');

  const remove = async (row: InvestigationSummary) => {
    try {
      await deleteInvestigation(row.investigation_id);
      setState(previous => ({ ...previous, rows: previous.rows.filter(item => item.investigation_id !== row.investigation_id) }));
      toast('Conversation deleted. The incident record and audit trail are unchanged.');
    } catch (reason) {
      toast(reason instanceof Error ? reason.message : 'Could not delete this conversation.', 'err');
    }
  };

  const counts = useMemo(() => ({
    total: state.rows.length,
    open: state.rows.filter(isOpen).length,
    resolved: state.rows.filter(row => row.incident_id && !isOpen(row)).length,
    messages: state.rows.reduce((sum, row) => sum + (row.message_count || 0), 0),
  }), [state.rows]);

  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return state.rows.filter(row => {
      if (filter === 'open' && !isOpen(row)) return false;
      if (filter === 'resolved' && !(row.incident_id && !isOpen(row))) return false;
      if (filter === 'none' && row.incident_id) return false;
      return !needle || [row.title, row.incident_id, row.operator_id].some(value => String(value || '').toLowerCase().includes(needle));
    }).map(row => ({ ...row, id: row.investigation_id }));
  }, [state.rows, query, filter]);

  return <Page title="Investigation History" sub="Every Console conversation is saved automatically. Open one to continue where you left off." actions={<><BackButton /><button className="btn" onClick={() => setReload(n => n + 1)}>Refresh</button></>}>
    {state.status !== 'ready'
      ? <ResourceMessage resource={state} retry={() => setReload(n => n + 1)} />
      : <>
        <div className="pl-stats" aria-label="Conversation summary">
          <div className="pl-stat"><span>Conversations</span><b>{counts.total}</b></div>
          <div className="pl-stat"><span>On open incidents</span><b>{counts.open}</b></div>
          <div className="pl-stat"><span>On closed incidents</span><b>{counts.resolved}</b></div>
          <div className="pl-stat"><span>Messages saved</span><b>{counts.messages}</b></div>
        </div>
        <div className="pl-toolbar">
          <label className="pl-search"><Search size={15} />
            <input aria-label="Search conversations" placeholder="Search by question, incident or operator" value={query} onChange={event => setQuery(event.target.value)} /></label>
          <select aria-label="Filter by incident" value={filter} onChange={event => setFilter(event.target.value as Filter)}>
            <option value="all">All conversations</option>
            <option value="open">Open incidents</option>
            <option value="resolved">Closed incidents</option>
            <option value="none">No incident linked</option>
          </select>
        </div>
        <DataTable rows={visible} size={10} onRow={openRow}
          empty={state.rows.length ? 'No conversations match the current search.' : 'No investigations saved yet. Start one in the Console.'} cols={[
            { k: 'title', h: 'Conversation', r: (row: any) => <div className="pl-conv"><span className="pl-conv-icon"><MessageSquare size={15} /></span>
              <div><b>{row.title}</b><p className="tiny muted">{row.message_count} messages · {row.operator_id}</p></div></div> },
            { k: 'incident_id', h: 'Incident', r: (row: any) => row.incident_id ? <Link className="pl-mono" to={`/incidents/${encodeURIComponent(row.incident_id)}`} onClick={event => event.stopPropagation()}>{row.incident_id}</Link> : '—' },
            { k: 'incident_status', h: 'Incident status', r: (row: any) => row.incident_id
              ? <div><Badge v={String(row.incident_status || 'unknown').toLowerCase()}>{String(row.incident_status || 'unknown')}</Badge>
                {row.resolution_reason && <p className="tiny muted">{String(row.resolution_reason).replace(/_/g, ' ')}</p>}</div>
              : '—' },
            { k: 'updated_at', h: 'Last updated', r: (row: any) => <div><span>{fmt(row.updated_at)}</span><p className="tiny muted">Started {fmt(row.created_at)}</p></div> },
            { k: 'actions', h: 'Actions', cls: 'pl-actions-col', r: (row: any) => <div className="pl-actions">
              <button className="btn sm pl-view" type="button" aria-label={`View conversation ${row.title}`} onClick={event => { event.stopPropagation(); openRow(row); }}><Eye size={14} />View</button>
              <button className="btn sm pl-delete" type="button" aria-label={`Delete conversation ${row.title}`}
                disabled={!canDelete(row)} title={canDelete(row) ? 'Delete this saved conversation' : 'Only the operator who saved it, or an owner, can delete it'}
                onClick={event => { event.stopPropagation(); setPendingDelete(row); }}><Trash2 size={14} />Delete</button>
            </div> },
          ]} />
      </>}
    {pendingDelete && <Confirm title="Delete this conversation?" label="Delete"
      text={`“${pendingDelete.title}” will be removed from Investigation History. The incident record and its audit trail are not affected.`}
      onOk={() => remove(pendingDelete)} onClose={() => setPendingDelete(null)} />}
  </Page>;
}

export function InvestigationDetail() {
  const nav = useNavigate();
  useEffect(() => { nav('/investigations', { replace: true }); }, []);
  return null;
}