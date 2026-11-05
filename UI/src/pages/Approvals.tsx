import { useState } from 'react';
import { Page, DataTable, Badge, Tabs, Modal, ResourceMessage } from '../ui';
import { decide, refresh, useStore } from '../store';
import { fmt } from '../data';
import { AnimatedTimeline, BackButton, eventsFromAudit } from '../components';

const statuses = ['PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'CANCELLED'];
const REMEDIATION_EVENT = /approval|remediat|action_|execut|verif|rollback|restart|scale/i;

export default function Approvals() {
  const state = useStore();
  const canDecide = state.permissions.includes('remediation');
  const canSelfApprove = state.permissions.includes('owner');
  const [tab, setTab] = useState('PENDING');
  const [selectedId, setSelectedId] = useState('');
  const [decision, setDecision] = useState<'approved' | 'rejected' | ''>('');
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const approvals = state.approvals.data || [];
  const current = approvals.find(item => item.approval_id === selectedId);
  const isRequester = Boolean(
    current && state.operatorId && current.requested_by === state.operatorId
      && !canSelfApprove,
  );
  const rows = approvals.filter(item => item.status === tab);

  const submitDecision = async () => {
    if (!current || !decision || !reason.trim() || busy) return;
    if (decision === 'approved' && isRequester) {
      setError('A different authorized operator must approve this request.');
      return;
    }
    setBusy(true);
    setError('');
    try {
      await decide(
        String(current.approval_id),
        decision,
        reason.trim(),
      );
      setDecision('');
      setReason('');
    } catch (reasonError) {
      setError(reasonError instanceof Error ? reasonError.message : 'Decision request failed.');
      void refresh('approvals');
      void refresh('audit');
    } finally {
      setBusy(false);
    }
  };

  return <Page title="Approvals & Remediation" sub="Approving starts the guarded execution workflow. It requires remediation permission and fresh backend safety checks." actions={<><BackButton /><button className="btn" onClick={() => { void refresh('approvals'); void refresh('audit'); }}>Refresh</button></>}>
    <Tabs tabs={[...statuses.map(status => [status, status.toLowerCase()]), ['HISTORY', 'history']]} v={tab} set={setTab} />
    {tab === 'HISTORY'
      ? <div className="grid g2">
        <div className="panel"><h3>Remediation & approval history</h3>
          <p className="tiny muted">Every approval request, decision, execution and verification recorded by the backend, newest first.</p>
          <div className="histbox"><AnimatedTimeline events={eventsFromAudit((state.audit.data || []).filter(event => REMEDIATION_EVENT.test(String(event.event_type))))} empty="No approval or remediation events have been recorded." /></div></div>
        <div className="panel"><h3>All approval requests ({approvals.length})</h3>
          <div className="histbox">{approvals.length ? approvals.map(item => <div className="qa blk" key={String(item.approval_id)} role="button" tabIndex={0} onClick={() => { setTab(String(item.status)); setSelectedId(String(item.approval_id)); }}>
            <Badge v={String(item.status).toLowerCase()}>{String(item.status)}</Badge> <b>{String((item.proposed_action as any)?.action ?? 'action')}</b>
            <p className="tiny muted">{String(item.incident_ref)} · {fmt(item.requested_at as string)} · by {String(item.requested_by)}{item.decided_by ? ` · decided by ${String(item.decided_by)}` : ''}</p></div>) : <p className="muted">No approvals recorded.</p>}</div></div>
      </div>
      : state.approvals.status !== 'ready'
      ? <ResourceMessage resource={state.approvals} retry={() => void refresh('approvals')} />
      : <DataTable rows={rows} onRow={(row: any) => setSelectedId(String(row.approval_id))} empty={tab === 'PENDING'
        ? 'No pending approvals. Requests for resolved or missing incidents are cancelled and retained in history.'
        : `No ${tab.toLowerCase()} approvals returned.`} cols={[
        { k: 'approval_id', h: 'ID' }, { k: 'incident_ref', h: 'Incident' },
        { k: 'proposed_action', h: 'Proposed action', r: (row: any) => JSON.stringify(row.proposed_action) },
        { k: 'status', h: 'Status', r: (row: any) => <Badge v={String(row.status || 'unknown').toLowerCase()}>{String(row.status || 'unknown')}</Badge> },
        { k: 'requested_by', h: 'Requested by' },
        { k: 'requested_at', h: 'Requested', r: (row: any) => fmt(row.requested_at) },
        { k: 'expires_at', h: 'Expires', r: (row: any) => fmt(row.expires_at) },
      ]} />}
    {current && <Modal title={`${String(current.approval_id)} · ${String(current.status)}`} onClose={() => { setSelectedId(''); setDecision(''); setReason(''); setError(''); }}>
      <dl className="kv">
        <dt>Incident</dt><dd>{String(current.incident_ref)}</dd>
        <dt>Requested by</dt><dd>{String(current.requested_by)}</dd>
        <dt>Requested at</dt><dd>{fmt(current.requested_at as string)}</dd>
        <dt>Expires at</dt><dd>{fmt(current.expires_at as string)}</dd>
        <dt>Proposed action</dt><dd><pre>{JSON.stringify(current.proposed_action, null, 2)}</pre></dd>
        <dt>Context</dt><dd><pre>{JSON.stringify(current.context, null, 2)}</pre></dd>
        <dt>Previous decision</dt><dd>{String(current.decided_by || 'Not decided')} {current.decision_reason ? `· ${String(current.decision_reason)}` : ''}</dd>
      </dl>
      {current.status === 'PENDING' && <>
        <p className="muted">Approval starts the guarded execution workflow after authorization and fresh safety checks pass. A different authorized operator must approve this request unless the authenticated account has trusted owner permission. Automatic execution remains disabled.</p>
        {isRequester && <p className="err" role="alert">
          This request was created by your operator account. You may reject it, but a different authorized operator must approve it.
        </p>}
        {canSelfApprove && current.requested_by === state.operatorId && <p className="tiny muted" role="status">
          This trusted owner account may approve its own request. Fresh backend policy and safety checks still apply.
        </p>}
        {!canDecide && <p className="muted">This account is read-only and cannot submit approval decisions.</p>}
        {canDecide && <>
        <label className="field">Decision reason (required)<textarea maxLength={500} value={reason} onChange={event => setReason(event.target.value)} /></label>
        {error && <p className="err" role="alert">{error}<button className="btn sm" onClick={() => void refresh('approvals')}>Refresh state</button></p>}
        {!decision
          ? <div className="row end gap"><button className="btn danger" onClick={() => setDecision('rejected')}>Reject</button><button className="btn primary" disabled={isRequester} title={isRequester ? 'A different authorized operator must approve this request' : ''} onClick={() => setDecision('approved')}>Approve & execute</button></div>
          : <div className="row end gap"><span>Submit {decision === 'approved' ? 'approval and start execution' : 'rejection'}?</span><button className="btn" onClick={() => setDecision('')}>Cancel</button><button className="btn primary" disabled={busy || !reason.trim()} onClick={() => void submitDecision()}>{busy ? 'Submitting…' : decision === 'approved' ? 'Confirm & execute' : 'Confirm rejection'}</button></div>}
        </>}
      </>}
      {current.status === 'APPROVED' && <>
        <p className="muted">The backend recorded this approval and processed the guarded execution workflow. Review the audit history for its outcome.</p>
        <p className="tiny">Execution state: {String(current.execution_status || 'not reported').replace(/_/g, ' ')}</p>
        {!canDecide && <p className="muted">This account lacks the remediation permission.</p>}
      </>}
    </Modal>}
  </Page>;
}