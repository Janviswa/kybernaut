import { useState } from 'react';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Check, CircleDot, Loader2, X, Minus } from 'lucide-react';
import { Badge, Empty } from '../ui';
import { fmt } from '../data';
import { AnimatedTimeline, eventsFromAudit } from '../components';

type Rec = Record<string, unknown>;
const rec = (value: unknown): Rec | null => (value && typeof value === 'object' && !Array.isArray(value) ? value as Rec : null);
const text = (value: unknown, fallback = '—') => (value === null || value === undefined || value === '' ? fallback : String(value));
const list = (value: unknown): string[] => (Array.isArray(value) ? value.map(item => (typeof item === 'string' ? item : JSON.stringify(item))) : []);
export const optionExecutionAvailable = (item: Rec) =>
  item.executable === true && item.policy_available !== false;
const SERVICE_WORKLOAD: Record<string, { work: string; effect: string }> = {
  'order-service': { work: 'order placement and fulfilment requests', effect: 'Customers can place and track orders again' },
  'payment-service': { work: 'payment authorisation requests', effect: 'Checkout payments can be authorised again' },
  'user-service': { work: 'account and profile lookups', effect: 'Sign-ins and profile reads work again' },
  'inventory-service': { work: 'stock and availability queries', effect: 'Stock levels are served correctly again' },
};
type Recovery = { title: string; how: (s: string, w: string) => string; fix: (s: string, e: string) => string };
const SERVICE_RECOVERY_DETAILS: Record<string, Recovery> = {
  high_latency: {
    title: 'Restore normal response times',
    how: (s, w) => `Removes the extra processing delay that is holding back ${w} on ${s}, so each request is handled as soon as it arrives.`,
    fix: (s, e) => `p95 latency on ${s} drops back to its baseline and the backlog drains. ${e}; the incident closes once latency is verified.`,
  },
  http_5xx: {
    title: 'Stop the HTTP 503 error responses',
    how: (s, w) => `Lifts the fault that rejects ${w} with HTTP 503, handing them back to the normal request handler on ${s}.`,
    fix: (s, e) => `A test business request to ${s} returns a success status instead of 503. ${e}; the incident closes after that check passes.`,
  },
  request_failure: {
    title: 'Stop the HTTP 500 request failures',
    how: (s, w) => `Clears the error condition that makes ${w} fail with HTTP 500, so ${s} runs its normal request handling again.`,
    fix: (s, e) => `Requests to ${s} complete successfully and the 500 rate returns to zero. ${e}; the incident closes once this is confirmed.`,
  },
  service_unavailable: {
    title: 'Bring the service back into rotation',
    how: (s, w) => `Re-enables ${s} so ${w} stop being refused with HTTP 503 and are routed to a working handler.`,
    fix: (s, e) => `The health check on ${s} passes and live traffic is accepted again. ${e}; the incident closes after recovery is verified.`,
  },
};
const incidentRecoveryOption = (option: Rec, incidentType: string, service: string): Rec => {
  if (option.action !== 'clear_local_simulation') return option;
  const workload = SERVICE_WORKLOAD[service] ?? { work: 'incoming requests', effect: 'The service is healthy again' };
  const recovery: Recovery = SERVICE_RECOVERY_DETAILS[incidentType] ?? {
    title: 'Restore normal service',
    how: (s, w) => `Removes the active fault so ${w} on ${s} are handled normally.`,
    fix: (s, e) => `${s} reports healthy and the incident behaviour stops. ${e}; the incident closes after recovery is verified.`,
  };
  return {
    ...option,
    title: `${recovery.title} on ${service}`,
    rationale: `Targeted fix for ${incidentType.replace(/_/g, ' ')} on ${service}.`,
    steps: [recovery.how(service, workload.work), recovery.fix(service, workload.effect)],
    risk: `Low. Only ${service} is touched; other services are unaffected, and approval is required before it runs.`,
  };
};
const StepList = ({ option }: { option: Rec }) => {
  if (!Array.isArray(option.steps)) return null;
  return option.action === 'clear_local_simulation' ||
    option.action === 'migrate_payment_history_and_deploy'
    ? <ul className="rem-points">{list(option.steps).map((step, i) => <li key={i}>{step}</li>)}</ul>
    : <ul className="tiny">{list(option.steps).map((step, i) => <li key={i}><code>{step}</code></li>)}</ul>;
};
const optionUnavailableMessage = (item: Rec) =>
  item.policy_available === false
    ? text(item.policy_detail, text(item.policy_reason, 'blocked by policy'))
    : text(item.runbook_reason || item.runbook_status, 'not supported by the runbook');
export const replicaPrerequisiteMessage = (item: Rec) => {
  if (item.runbook_reason !== 'minimum_ready_replicas_not_met') return null;
  const required = rec(item.runbook_prerequisites)?.min_ready_replicas;
  const observed = item.observed_available_replicas;
  if (typeof required !== 'number' || typeof observed !== 'number') return null;
  const requiredLabel = `${required} ready replica${required === 1 ? '' : 's'}`;
  const observedLabel = `${observed} available replica${observed === 1 ? '' : 's'}`;
  return `Runbook requires at least ${requiredLabel}; observed ${observedLabel}.`;
};

type Execute = (approvalId: string, reason: string) => Promise<void>;
type Props = {
  result: Rec;
  approvals: Rec[];
  canDecide: boolean;
  onExecute: Execute;
};

/** One rounded bubble per remediation suggestion, each with its own Execute button. */
export function InvestigationBubbles({ result, approvals, canDecide, onExecute }: Props) {
  const classification = rec(result.classification);
  const rca = rec(result.rca);
  const proposal = rec(result.proposal);
  const decision = rec(result.policy_decision);
  const runbook = rec(result.runbook_resolution);
  const approvalId = typeof result.approval_id === 'string' ? result.approval_id : null;
  const live = approvals.find(item => item.approval_id === approvalId);
  const status = String(live?.status ?? (approvalId ? 'PENDING' : 'NONE'));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const rules = Array.isArray(decision?.rules) ? decision!.rules as Rec[] : [];
  const risks = list(runbook?.documented_risks);
  const advisory = typeof rca?.recommended_action === 'string' && rca.recommended_action !== proposal?.action
    ? String(rca.recommended_action) : null;

  const execute = async () => {
    if (!approvalId || busy) return;
    setBusy(true); setError('');
    try {
      await onExecute(approvalId, 'Approved from the Console Execute action.');
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : 'The execution request failed.');
    } finally { setBusy(false); }
  };

  return <div className="bubbles">
    <div className="ai bubble">
      <b>Diagnosis</b>
      <dl className="kvl">
        <dt>Incident type</dt><dd>{text(classification?.incident_type, 'unknown')} · confidence {text(classification?.confidence, 'n/a')}</dd>
        <dt>Observed facts</dt><dd>{list(classification?.evidence).join(' · ') || 'No facts recorded'}</dd>
        <dt>Root cause</dt><dd>{rca ? text(rca.root_cause) : `No RCA result (${text(result.rca_status, 'not reported')}${result.rca_error ? `: ${text(result.rca_error)}` : ''})`}</dd>
        {rca && <><dt>RCA confidence</dt><dd>{text(rca.confidence)}</dd>
          <dt>RCA evidence</dt><dd>{list(rca.evidence).join(' · ') || '—'}</dd></>}
      </dl>
      <p className="tiny muted">RCA is advisory and does not establish a verified cause.</p>
    </div>

    {proposal ? <div className="ai bubble rem">
      <div className="row between gap"><b>Remediation suggestion</b><Badge v={status === 'PENDING' ? 'pending' : status === 'APPROVED' ? 'approved' : status === 'REJECTED' ? 'rejected' : 'info'}>{status.toLowerCase()}</Badge></div>
      <p><b>{text(proposal.action).replace(/_/g, ' ')}</b> on <code>{text(proposal.resource)}</code>
        {proposal.target_replicas != null ? ` → ${text(proposal.target_replicas)} replicas` : ''}</p>
      <p className="muted">{text(proposal.rationale, '')}</p>
      <p className="tiny">Policy: {decision ? (decision.allowed ? 'allowed' : 'blocked') : 'not evaluated'}
        {decision?.requires_approval ? ' · human approval required' : ''}</p>
      {rules.length > 0 && <details open={decision?.allowed === false}><summary className="tiny">Safety checks ({rules.filter(r => r.passed).length}/{rules.length} passed)</summary>
        <ul className="tiny">{rules.map((rule, index) => <li key={index}>{rule.passed ? '✓' : '✗'} {text(rule.id)} — {text(rule.detail)}</li>)}</ul></details>}
      {risks.length > 0 && <ul className="tiny muted">{risks.map((item, index) => <li key={index}>Risk: {item}</li>)}</ul>}
      {status === 'PENDING' && approvalId
        ? <>
          <button className="btn primary" disabled={!canDecide || busy} title={canDecide ? '' : 'Requires the remediation permission'}
            onClick={() => void execute()}>{busy ? 'Executing…' : 'Execute'}</button>
          {!canDecide && <p className="tiny muted">Your account lacks the remediation permission.</p>}
          {error && <p className="err" role="alert">{error}</p>}
        </>
        : <p className="tiny muted">{decision?.allowed === false
          ? 'No approval was created because a safety check blocked this action. No remediation was run.'
          : status === 'NONE' ? 'No approval request exists for this suggestion.' : `This approval is ${status.toLowerCase()}.`} <Link to="/incidents">View in Incidents &amp; Alerts</Link></p>}
    </div> : <div className="ai bubble">
      <b>No automated remediation</b>
      <p className="muted">The agent did not propose an executable action ({text(result.plan_note, text(result.status))}). A human should review the evidence above.</p>
    </div>}

    {advisory && <div className="ai bubble"><b>Advisory suggestion (manual)</b>
      <p><b>{advisory.replace(/_/g, ' ')}</b></p>
      <p className="tiny muted">Suggested by the RCA model. It is not executable from here because it did not pass the deterministic plan, runbook and policy checks.</p></div>}
  </div>;
}

/** Right-panel "Remediation" tab: everything is read from stored records. */
export function RemediationPanel({ approvals, events, policy, recs, options = [], resolved = false,
  outcome, incidentType = '', assessmentRequired = false, canAssess = false, onAssess,
  resolvedMessage = 'Resolved: recovery was verified.', readinessNote = '',
  canRun = false, busy = false, onExecute, executeInChat = false }: {
  approvals: Rec[]; events: Rec[]; policy: Rec | null; recs: { title: string; detail?: string }[];
  incidentType?: string;
  readinessNote?: string;
  options?: Rec[]; resolved?: boolean; canRun?: boolean; busy?: boolean; onExecute?: (option: Rec) => void;
  /** When true the Execute buttons live in the Console chat, so they are not repeated here. */
  executeInChat?: boolean;
  assessmentRequired?: boolean; canAssess?: boolean; onAssess?: () => void;
  resolvedMessage?: string;
  outcome?: { status: 'resolved' | 'dry_run' | 'not_resolved'; detail: string };
}) {
  const history = events.filter(event => /approval|remediat|action_|execut|verif|rollback|restart|scale|proposed/i.test(String(event.event_type)));
  const displayOptions = options.map(item => incidentRecoveryOption(
    item, incidentType, text(item.resource, 'the affected service'),
  ));
  const hasExecutableOption = displayOptions.some(item => item.kind === 'executable');
  return <>
    {resolved && <div className="resolvedbar" role="status"><Check size={16} /> {resolvedMessage}</div>}
    {outcome && <div className="rem-card" role="status">
      <div className="row between gap"><b>Latest remediation outcome</b>
        <Badge v={outcome.status}>{outcome.status === 'dry_run'
          ? 'DRY RUN · NOT EXECUTED'
          : outcome.status === 'resolved' ? 'RESOLVED' : 'NOT RESOLVED'}</Badge></div>
      <p className="tiny muted">{outcome.detail}</p>
    </div>}
    {assessmentRequired && <div className="rem-card" role="status">
      <b>Remediation options have not been checked</b>
      <p className="tiny muted">Check current evidence to load applicable options. This does not execute remediation; a confirmed-cleared condition may be reconciled as resolved.</p>
      {onAssess && <button className="btn primary" disabled={!canAssess || busy} onClick={onAssess}>
        {busy ? 'Checking options…' : 'Check remediation options'}
      </button>}
    </div>}
    {displayOptions.length > 0 && !hasExecutableOption && <div className="rem-card" role="status">
      <b>{resolved ? 'Incident resolved · no Execute action' : 'No executable remediation is available yet'}</b>
      <p className="tiny muted">{resolved
        ? 'Fresh evidence confirmed this condition cleared. The saved suggestions below are informational and should not be run for this resolved incident.'
        : text(readinessNote, 'No action passed the current evidence, runbook, and policy checks. Review the recovery guidance below.')}</p>
    </div>}
    {displayOptions.length > 0 && <><div className="rh"><h3>Available options</h3></div>
      {displayOptions.map((item, index) => <div className="rem-card" key={index}>
        <div className="row between gap"><b>{text(item.title)}</b>
          <Badge v={item.kind === 'executable' ? (optionExecutionAvailable(item) ? 'healthy' : 'blocked') : 'info'}>{item.kind === 'executable' ? (optionExecutionAvailable(item) ? 'can execute' : 'not available') : 'manual'}</Badge></div>
        {item.rationale && <p className="tiny muted">{text(item.rationale)}</p>}
        <StepList option={item} />
        {item.risk && <p className="tiny muted">Risk: {text(item.risk)}</p>}
        {item.kind === 'executable' && replicaPrerequisiteMessage(item) &&
          <p className="tiny muted">{replicaPrerequisiteMessage(item)}</p>}
        {item.kind === 'executable' && executeInChat && optionExecutionAvailable(item) && !resolved &&
          <p className="tiny muted">Approve and run this from the Console chat.</p>}
        {item.kind === 'executable' && !executeInChat && <button className="btn primary" disabled={!optionExecutionAvailable(item) || !canRun || resolved || busy}
          title={!optionExecutionAvailable(item) ? optionUnavailableMessage(item)
            : resolved ? 'This incident is already resolved' : !canRun ? 'Requires read access' : ''}
          onClick={() => onExecute?.(item)}>Execute</button>}
        {item.kind === 'executable' && item.policy_available === false &&
          <p className="tiny muted">Not available now: {optionUnavailableMessage(item)}</p>}
      </div>)}</>}
    <div className="rh"><h3>Approvals</h3></div>
    {approvals.length ? approvals.map(item => <div className="rem-card" key={String(item.approval_id)}>
      <div className="row between gap"><b>{text(rec(item.proposed_action)?.action).replace(/_/g, ' ')}</b>
        <Badge v={String(item.status).toLowerCase()}>{String(item.status)}</Badge></div>
      <p className="tiny muted">{text(rec(item.proposed_action)?.resource)} · requested {fmt(item.requested_at as string)} by {text(item.requested_by)}</p>
      {item.decided_by ? <p className="tiny">Decided by {text(item.decided_by)} {item.decided_at ? `· ${fmt(item.decided_at as string)}` : ''}</p> : null}
    </div>) : <Empty text="No approval has been requested for this incident." />}
    {recs.length > 0 && <><div className="rh"><h3>Recommendations</h3></div>
      {recs.map((item, index) => <div className="rec" key={index}><b>{item.title}</b>{item.detail && <p className="muted">{item.detail}</p>}</div>)}</>}
    <div className="rh"><h3>Remediation history</h3></div>
    <AnimatedTimeline events={eventsFromAudit(history)} empty="No remediation events recorded yet." />
    <p className="tiny muted">Policy: {text(policy?.action, 'not reported')}. Remediation runs only after an authorized human approves it.</p>
  </>;
}


/* ---------- step-by-step progress ---------- */
export type Step = { label: string; status: 'done' | 'active' | 'pending' | 'error' | 'skipped'; detail?: string };

export function StepsBubble({ title, steps }: { title: string; steps: Step[] }) {
  const busy = steps.some(step => step.status === 'active');
  return <div className="ai bubble stepsbubble" role="status" aria-live="polite">
    <div className="row gap"><b>{title}</b>{busy && <span className="typing"><span /><span /><span /></span>}</div>
    <ol className="steps">
      {steps.map((step, index) => <li key={index} className={`step-${step.status}`} style={{ animationDelay: `${index * 80}ms` }}>
        <span className="step-ic" aria-hidden>
          {step.status === 'done' ? <Check size={14} /> : step.status === 'error' ? <X size={14} />
            : step.status === 'active' ? <Loader2 size={14} className="spin" /> : step.status === 'skipped' ? <Minus size={14} /> : <CircleDot size={14} />}
        </span>
        <div><span>{step.label}</span>{step.detail && <p className="tiny muted">{step.detail}</p>}</div>
      </li>)}
    </ol>
  </div>;
}

/* ---------- remediation option set: one bubble per option ---------- */
/**
 * `inline` is the Console conversation mode: only the options that can actually be
 * executed right now are shown, with a question ("shall I run it?"), Execute and
 * Not now. Manual guidance stays in the Remediation tab.
 */
export function OptionBubbles({ data, canRun, resolved, busy, onExecute, showExecute = true, inline = false, onDismiss, analysing = false }: {
  data: Rec; canRun: boolean; resolved: boolean; busy: boolean; onExecute: (option: Rec) => void | Promise<void>;
  showExecute?: boolean; inline?: boolean; onDismiss?: () => void;
  /** The investigation is still running: show the Execute button, locked until it finishes. */
  analysing?: boolean;
}) {
  const [dismissed, setDismissed] = useState(false);
  const [executing, setExecuting] = useState(false);
  const run = (option: Rec) => {
    setExecuting(true);
    void Promise.resolve(onExecute(option)).finally(() => setExecuting(false));
  };
  const options = (Array.isArray(data.options) ? data.options : [])
    .map(rec)
    .filter((item): item is Rec => item !== null)
    .map(item => incidentRecoveryOption(
      item,
      text(data.incident_type, 'unknown'),
      text(item.resource, 'the affected service'),
    ));
  const hasIncidentRecoveryOption = options.some(
    item => item.action === 'clear_local_simulation',
  );
  const classification = rec(data.classification);
  const incidentAlert = text(data.incident_alert, '');
  const affectedService = text(data.service, '');
  const executable = options.filter(item => item.kind === 'executable');
  const availableCount = executable.filter(optionExecutionAvailable).length;
  const manual = options.filter(item => item.kind !== 'executable');

  if (inline) {
    const runnable = executable.filter(optionExecutionAvailable);
    const blocked = executable.find(item => !optionExecutionAvailable(item));
    if (runnable.length === 0) {
      return <div className="ai bubble">
        <b>{resolved ? 'This incident is already resolved' : 'No safe automatic action is available for this alert'}</b>
        {(incidentAlert || affectedService) && <p className="tiny muted">
          {incidentAlert && <><b>Incident alert:</b> {incidentAlert}</>}
          {incidentAlert && affectedService && ' · '}
          {affectedService && <><b>Affected service:</b> {affectedService}</>}
        </p>}
        <p className="muted">{resolved
          ? 'Fresh evidence shows the condition cleared, so there is nothing to execute.'
          : blocked ? replicaPrerequisiteMessage(blocked) || optionUnavailableMessage(blocked)
            : text(data.note, 'No option passed the runbook and policy checks for this incident.')}</p>
        {manual.length > 0 && <p className="tiny muted">{manual.length} recovery guidance step{manual.length === 1 ? '' : 's'} for an operator {manual.length === 1 ? 'is' : 'are'} listed in the Remediation tab.</p>}
      </div>;
    }
    return <div className="bubbles">
      <div className="ai bubble">
        <div className="row between gap"><b>{runnable.length === 1 ? 'I found 1 action I can run' : `I found ${runnable.length} actions I can run`}</b>
          {data.live_evidence ? <Badge v="healthy">live evidence</Badge> : null}</div>
        <p className="muted">{text(data.incident_type, 'unknown').replace(/_/g, ' ')}{classification ? ` · confidence ${text(classification.confidence)}` : ''}. Only options that passed the runbook and safety checks are shown.</p>
        {(incidentAlert || affectedService) && <p className="tiny muted">
          {incidentAlert && <><b>Incident alert:</b> {incidentAlert}</>}
          {incidentAlert && affectedService && ' · '}
          {affectedService && <><b>Affected service:</b> {affectedService}</>}
        </p>}
        {data.note && !hasIncidentRecoveryOption
          ? <p className="tiny muted">{text(data.note)}</p> : null}
      </div>
      {runnable.map((option, index) => <div className="ai bubble option rem" key={text(option.option_id, String(index))} style={{ animationDelay: `${index * 70}ms` }}>
        <div className="row between gap"><b>{text(option.title)}</b><Badge v="healthy">can execute</Badge></div>
        {option.rationale ? <p className="muted">{text(option.rationale)}</p> : null}
        <StepList option={option} />
        <p className="tiny muted"><b>Risk:</b> {text(option.risk)}{option.selected ? ' · recommended' : ''}</p>
        {replicaPrerequisiteMessage(option) && <p className="tiny muted">{replicaPrerequisiteMessage(option)}</p>}
        {!dismissed && !resolved && <>
          <p className="rem-ask">{analysing ? 'Analysis is still running. Execute unlocks when it finishes.' : 'Shall I run this now? It needs your approval.'}</p>
          <div className="rem-actions">
            <button className="btn primary" disabled={!canRun || busy || analysing || executing} onClick={() => run(option)}
              title={analysing ? 'Waiting for the analysis to finish' : !canRun ? 'Requires the remediation permission' : ''}>
              {analysing ? <><Loader2 size={14} className="spin" aria-hidden />Analysing…</>
                : executing ? <><Loader2 size={14} className="spin" aria-hidden />Executing…</> : 'Execute'}
            </button>
            <button className="btn" disabled={busy || analysing} onClick={() => { setDismissed(true); onDismiss?.(); }}>Not now</button>
          </div>
          {!canRun && <p className="tiny muted">Your account lacks the remediation permission.</p>}
        </>}
        {!dismissed && resolved && <p className="tiny muted rem-resolved">This incident is already resolved, so there is nothing left to execute.</p>}
        {dismissed && <p className="tiny muted">Skipped. Nothing was run; you can still run it from the Remediation tab.</p>}
      </div>)}
      {manual.length > 0 && <p className="tiny muted">{manual.length} more manual guidance step{manual.length === 1 ? '' : 's'} for an operator {manual.length === 1 ? 'is' : 'are'} in the Remediation tab.</p>}
    </div>;
  }

  return <div className="bubbles">
    <div className="ai bubble">
      <div className="row between gap"><b>What the agent found</b>
        {data.live_evidence ? <Badge v="healthy">live evidence</Badge> : null}</div>
      <dl className="kvl">
        <dt>Incident type</dt><dd>{text(data.incident_type, 'unknown')}{classification ? ` · confidence ${text(classification.confidence)}` : ''}</dd>
        {incidentAlert && <><dt>Incident alert</dt><dd>{incidentAlert}</dd></>}
        {affectedService && <><dt>Affected service</dt><dd>{affectedService}</dd></>}
        {classification && <><dt>Observed facts</dt><dd>{list(classification.evidence).join(' · ') || 'None recorded'}</dd></>}
        <dt>Options</dt><dd>{options.length} remediation option{options.length === 1 ? '' : 's'} ({availableCount} executable, {manual.length} are operator guidance)</dd>
      </dl>
      {data.note && !hasIncidentRecoveryOption
        ? <p className="tiny muted">{text(data.note)}</p> : null}
    </div>
    {options.map((option, index) => {
      const isExec = option.kind === 'executable';
      const can = isExec && optionExecutionAvailable(option) && !resolved && canRun;
      const unavailableMessage = optionUnavailableMessage(option);
      return <div className={`ai bubble option ${isExec ? 'rem' : ''}`} key={text(option.option_id, String(index))} style={{ animationDelay: `${index * 70}ms` }}>
        <div className="row between gap">
          <b>Option {index + 1} · {text(option.title)}</b>
          <Badge v={isExec ? (optionExecutionAvailable(option) ? 'healthy' : 'blocked') : 'info'}>{isExec ? (optionExecutionAvailable(option) ? 'can execute' : 'not available') : 'manual'}</Badge>
        </div>
        {option.rationale ? <p className="muted">{text(option.rationale)}</p> : null}
        <StepList option={option} />
        <p className="tiny muted"><b>Risk:</b> {text(option.risk)}{option.selected ? ' · recommended' : ''}</p>
        {isExec && !optionExecutionAvailable(option) && <p className="tiny muted">Not available now: {unavailableMessage}</p>}
        {isExec && replicaPrerequisiteMessage(option) &&
          <p className="tiny muted">{replicaPrerequisiteMessage(option)}</p>}
        {isExec && showExecute && <button className="btn primary" disabled={!can || busy || resolved} onClick={() => onExecute(option)}
          title={resolved ? 'This incident is already resolved' : !canRun ? 'Requires the remediation permission' : ''}>Execute</button>}
      </div>;
    })}
  </div>;
}

/* ---------- grounded assistant answer ---------- */
const inline = (value: string) => value.split(/(\*\*[^*]+\*\*)/g).filter(Boolean).map((part, index) =>
  /^\*\*[^*]+\*\*$/.test(part) ? <b key={index}>{part.slice(2, -2)}</b> : <span key={index}>{part}</span>);

/** Renders the assistant's light markup: **bold**, "- " bullets, "## " headings, blank-line paragraphs. */
export function RichAnswer({ value }: { value: string }) {
  const blocks: ReactNode[] = [];
  let bullets: string[] = [];
  const flush = () => {
    if (!bullets.length) return;
    const items = bullets;
    bullets = [];
    blocks.push(<ul key={`ul-${blocks.length}`}>{items.map((item, index) =>
      <li key={index} className={/^(not recorded|missing|unknown|no )/i.test(item) ? 'warn' : ''}>{inline(item)}</li>)}</ul>);
  };
  value.split('\n').map(line => line.trim()).forEach(line => {
    if (!line) { flush(); return; }
    const bullet = line.match(/^[-•*]\s+(.*)$/);
    if (bullet) { bullets.push(bullet[1]); return; }
    flush();
    const heading = line.match(/^#{1,3}\s+(.*)$/);
    if (heading) { blocks.push(<p key={`h-${blocks.length}`} className="rich-h">{heading[1].replace(/\*\*/g, '')}</p>); return; }
    blocks.push(<p key={`p-${blocks.length}`} className={blocks.length === 0 ? 'rich-lead' : ''}>{inline(line)}</p>);
  });
  flush();
  return <div className="rich">{blocks}</div>;
}

export function AssistBubble({ result }: { result: Rec }) {
  const citations = list(result.citations);
  return <div className="ai bubble assist">
    <div className="row between gap"><b>Kybernaut</b>
      <Badge v={result.llm_used ? 'healthy' : 'pending'}>{result.llm_used ? 'AI · grounded in records' : 'records only'}</Badge></div>
    <RichAnswer value={text(result.answer)} />
    {result.insufficient_data === true && <p className="tiny muted">The stored records do not contain enough to answer fully.</p>}
    {citations.length > 0 && <p className="tiny muted">Sources: {citations.map(item => <span key={item} className="chip">{item.replace(/_/g, ' ')}</span>)}</p>}
    {result.note ? <p className="tiny muted">{text(result.note)}</p> : null}
  </div>;
}

/* ---------- tidy list-style answers ---------- */
const SEVERITIES = new Set(['critical', 'high', 'medium', 'low']);
const STATUSES = new Set(['open', 'resolved', 'remediating']);
export function AnswerSummary({ textValue }: { textValue: string }) {
  const lines = textValue.split('\n').map(line => line.trim()).filter(Boolean);
  const structured = lines.length > 1 && lines.filter(line => line.includes(' · ')).length >= Math.max(2, lines.length - 2);
  if (!structured) return <p style={{ whiteSpace: 'pre-wrap' }}>{textValue}</p>;
  const heading = lines.filter(line => !line.includes(' · '));
  const rows = lines.filter(line => line.includes(' · '));
  return <>
    {heading.map((line, index) => <p key={index} className="muted">{line}</p>)}
    <ul className="answerlist">
      {rows.map((line, index) => {
        const parts = line.split(' · ');
        return <li key={index}>
          {parts.map((part, i) => {
            const lower = part.toLowerCase();
            if (i === 0 && /^inc-/.test(part)) return <Link key={i} className="chip id" to={`/console?incident=${encodeURIComponent(part)}`}>{part}</Link>;
            if (SEVERITIES.has(lower)) return <Badge key={i} v={lower}>{part}</Badge>;
            if (STATUSES.has(lower)) return <Badge key={i} v={lower}>{part}</Badge>;
            return <span key={i} className="chip">{part}</span>;
          })}
        </li>;
      })}
    </ul>
  </>;
}