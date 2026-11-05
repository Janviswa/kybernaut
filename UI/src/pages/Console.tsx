import { KeyboardEvent, ReactNode, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import {
  AlertTriangle, Bell, ChevronRight, ExternalLink, History, Play, Send, Server, Sparkles,
  ScrollText,
} from 'lucide-react';
import { Badge, CopyBtn, Empty, LogoMark, Page, ResourceMessage, Tabs } from '../ui';
import { fmt, formatDuration, SERVICES } from '../data';
import {
  ApiError, apiRequest, getApprovals, getAuditEvents, getConfiguredServices, getIncident,
  getLiveTelemetry, getServiceHealth,
} from '../api';
import { decide, refresh, runWorkflow, toast, useStore } from '../store';
import {
  askConsole, getAgentStatus, getInvestigation, getRemediationOptions,
  saveInvestigation, executeLocalSimulation, executePaymentComposeRemediation,
} from '../api';
import { AnimatedTimeline, eventsFromAudit } from '../components';
import { AnswerSummary, AssistBubble, InvestigationBubbles, OptionBubbles, RemediationPanel, optionExecutionAvailable, replicaPrerequisiteMessage, StepsBubble } from './RemediationCards';
import type { Step } from './RemediationCards';
import { useIncident } from '../useIncident';
import { classifyConsoleIntent } from '../consoleIntents';
import { selectTelemetryServices, summarizeHighestP95 } from '../consoleTelemetry';
import { openIncidentsOf } from '../openIncidents';
import { atLeast, pause } from '../pacing';
import { AuditFlowCard, ReportCard, ServiceHealthCard } from '../consoleCards';
import type { ReportData, ServiceHealthData } from '../consoleCards';
import '../polish.css';

type Rec = Record<string, unknown>;
type Service = typeof SERVICES[number];
type TimelineState = {
  status: 'idle' | 'loading' | 'ready' | 'error';
  events: Rec[];
  error: string | null;
};
type Message =
  | { id: number; role: 'user'; text: string }
  | { id: number; role: 'agent'; text: string; tone?: 'error' }
  | { id: number; role: 'answer'; title: string; summary: string; data?: unknown; layout?: AnswerLayout }
  | { id: number; role: 'workflow'; service: string; result: Rec }
  | { id: number; role: 'remediation'; incidentId: string; result: Rec }
  | { id: number; role: 'steps'; title: string; steps: Step[] }
  | { id: number; role: 'options'; incidentId: string; data: Rec; analysing?: boolean }
  | { id: number; role: 'assist'; result: Rec }
  | { id: number; role: 'report'; report: ReportData };
type AnswerLayout = 'audit' | 'health' | 'service_health' | 'telemetry' | 'services' | 'timeline';
type NewMessage =
  | { role: 'user'; text: string }
  | { role: 'agent'; text: string; tone?: 'error' }
  | { role: 'answer'; title: string; summary: string; data?: unknown; layout?: AnswerLayout }
  | { role: 'workflow'; service: string; result: Rec }
  | { role: 'remediation'; incidentId: string; result: Rec }
  | { role: 'steps'; title: string; steps: Step[] }
  | { role: 'options'; incidentId: string; data: Rec; analysing?: boolean }
  | { role: 'assist'; result: Rec }
  | { role: 'report'; report: ReportData };

const rec = (value: unknown): Rec | null =>
  value && typeof value === 'object' && !Array.isArray(value) ? value as Rec : null;
const text = (value: unknown, fallback = '—') =>
  value === null || value === undefined || value === '' ? fallback : String(value);
const incidentRecordText = (value: unknown) => String(value || '')
  .replace(/^LOCAL SIMULATION:\s*/i, '')
  .replace(/\bthe local (order|payment|user|inventory) service\b/gi, 'the $1 service')
  .replace(/\bsimulated fault\b/gi, 'service fault')
  .replace(/\bsimulated\b|\bsimulator\b/gi, '')
  .replace(/\bsimulation\b/gi, 'service')
  .replace(/\s+/g, ' ')
  .trim();
const isDemo = (incident?: Rec | null) =>
  incident?.source === 'demo' || rec(incident?.source_evidence)?.simulation === 'demo';
const DEMO_NAMESPACE = 'kybernaut-lab';

type RecItem = { title: string; detail?: string };
const newChatId = () => `chat-${Math.random().toString(16).slice(2, 10)}${Date.now().toString(16)}`.slice(0, 24);

function collectRecs(policy: Rec | null, diagnosis: Rec | null): RecItem[] {
  const items: RecItem[] = [];
  if (policy?.action) {
    items.push({
      title: text(policy.action),
      detail: [policy.reason, policy.rationale].filter(Boolean).map(String).join(' · ') || undefined,
    });
  }
  const list = diagnosis?.recommendations;
  if (Array.isArray(list)) {
    list.forEach(entry => {
      const item = rec(entry);
      if (item) {
        items.push({
          title: text(item.title || item.action || item.recommendation || item.name, 'Recommendation'),
          detail: text(item.detail || item.description || item.reason, '') || undefined,
        });
      } else if (typeof entry === 'string') items.push({ title: entry });
    });
  }
  return items;
}

const Row = ({ k, children }: { k: string; children: ReactNode }) =>
  <><dt>{k}</dt><dd>{children}</dd></>;

const Json = ({ value }: { value: unknown }) =>
  <pre className="json" tabIndex={0}>{JSON.stringify(value, null, 2)}</pre>;

export default function Console() {
  const [params] = useSearchParams();
  const nav = useNavigate();
  const store = useStore();
  const incidentId = params.get('incident');
  const requestedService = params.get('service');
  const messageId = useRef(0);
  const requestLock = useRef(false);
  const executeLock = useRef(false);
  const endRef = useRef<HTMLDivElement>(null);
  const [service, setService] = useState<Service>(
    SERVICES.includes(requestedService as Service) ? requestedService as Service : SERVICES[0],
  );
  const [tab, setTab] = useState('investigation');
  const [draft, setDraft] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [running, setRunning] = useState(false);
  const [timeline, setTimeline] = useState<TimelineState>({ status: 'idle', events: [], error: null });
  const [timelineReload, setTimelineReload] = useState(0);
  const timelineFor = useRef<string | null>(null);
  const pendingSafeFollowUp = useRef<string | null>(null);
  const investigationParam = params.get('investigation');
  const hydrated = useRef(!investigationParam);
  const [chatId, setChatId] = useState(() => investigationParam || newChatId());
  const [resolvedIds, setResolvedIds] = useState<string[]>([]);
  const [executionOutcomes, setExecutionOutcomes] = useState<
    Record<string, { status: 'resolved' | 'dry_run' | 'not_resolved'; detail: string }>
  >({});
  const [remediationModes, setRemediationModes] = useState<
    Record<string, { mode: string | null; error: string | null }>
  >({});

  const canRun = store.permissions.includes('read');
  const incidents = store.incidents.data || [];
  // Right-hand list: same definition of "open" as the sidebar badge and the bell.
  const openIncidentList = openIncidentsOf(incidents);
  const loaded = useIncident(incidentId);
  // Keep showing the last loaded record while a refresh is in flight, so the console
  // never unmounts to a blank/loading screen after a run, approval or execution.
  const selected = incidentId && loaded.incident && String(loaded.incident.incident_id) === incidentId
    && loaded.status !== 'error' && loaded.status !== 'notfound'
    ? loaded.incident : undefined;
  const selectedOptions = selected ? [...messages].reverse().find(item =>
    item.role === 'options' && item.incidentId === String(selected.incident_id)) as
    Extract<Message, { role: 'options' }> | undefined : undefined;
  useEffect(() => {
    if (selectedOptions) setTab('remediation');
  }, [incidentId, selectedOptions?.id]);
  const latestReal = incidents.find(item => !isDemo(item));
  const latestWorkflow = [...messages].reverse().find(m => m.role === 'workflow') as
    Extract<Message, { role: 'workflow' }> | undefined;

  useEffect(() => {
    if (SERVICES.includes(requestedService as Service)) setService(requestedService as Service);
  }, [requestedService]);

  // Persisted audit timeline for the selected incident. Read-only.
  useEffect(() => {
    let active = true;
    if (!incidentId || !selected) {
      timelineFor.current = null;
      setTimeline({ status: 'idle', events: [], error: null });
      return () => { active = false; };
    }
    // Keep the events already on screen while refreshing the same incident.
    setTimeline(previous => timelineFor.current === incidentId && previous.events.length
      ? { ...previous, error: null }
      : { status: 'loading', events: [], error: null });
    apiRequest<{ incident_id: string; events: Rec[] }>(
      `/incidents/${encodeURIComponent(incidentId)}/timeline`,
    ).then(response => {
      if (!Array.isArray(response.events)) throw new Error('The backend returned an unexpected timeline response.');
      if (active) { timelineFor.current = incidentId; setTimeline({ status: 'ready', events: response.events, error: null }); }
    }).catch(reason => {
      if (active) {
        setTimeline(previous => previous.events.length && timelineFor.current === incidentId
          ? { ...previous, error: null }
          : {
          status: 'error', events: [],
          error: reason instanceof Error ? reason.message : 'Timeline request failed.',
        });
      }
    });
    return () => { active = false; };
  }, [incidentId, Boolean(selected), timelineReload]);

  useEffect(() => { endRef.current?.scrollIntoView?.({ block: 'end' }); }, [messages, running]);

  // Reopen a saved conversation (Investigation History).
  useEffect(() => {
    if (!investigationParam) return;
    let active = true;
    getInvestigation(investigationParam).then(saved => {
      if (!active) return;
      setChatId(saved.investigation_id);
      setMessages(saved.messages as unknown as Message[]);
      messageId.current = Math.max(0, ...saved.messages.map(item => Number(item.id) || 0));
      hydrated.current = true;
      if (saved.incident_id && !incidentId) {
        nav(`/console?incident=${encodeURIComponent(saved.incident_id)}&investigation=${encodeURIComponent(saved.investigation_id)}`, { replace: true });
      }
    }).catch(reason => {
      hydrated.current = true;
      toast(reason instanceof Error ? reason.message : 'Could not load the saved investigation.');
    });
    return () => { active = false; };
  }, [investigationParam]);

  // Every conversation is saved to the backend automatically (debounced).
  useEffect(() => {
    if (!messages.length || !hydrated.current) return;
    const first = messages.find(item => item.role === 'user') as { text?: string } | undefined;
    const title = (first?.text || (incidentId ? `Incident ${incidentId}` : 'Investigation')).slice(0, 120);
    const timer = window.setTimeout(() => {
      saveInvestigation(chatId, { title, incident_id: incidentId, messages }).catch(() => undefined);
    }, 900);
    return () => window.clearTimeout(timer);
  }, [messages, chatId, incidentId]);

  const push = (message: NewMessage): number => {
    const id = ++messageId.current;
    setMessages(list => [...list, { ...message, id } as Message]);
    return id;
  };
  const updateMessage = (id: number, patch: Record<string, unknown>) =>
    setMessages(list => list.map(item => (item.id === id ? { ...item, ...patch } as Message : item)));

  // Explicit operator action only. Never invoked on load, refresh or selection.
  const runAdvisory = async (target: Service, prompt?: string) => {
    if (requestLock.current) return;
    requestLock.current = true;
    push({ role: 'user', text: prompt || `Run the advisory workflow for ${target}` });
    setRunning(true);
    setTab('investigation');
    try {
      const result = await runWorkflow(target);
      push({ role: 'workflow', service: target, result });
      toast('Advisory response received from the backend.');
    } catch (reason) {
      push({
        role: 'agent', tone: 'error',
        text: `${reason instanceof Error ? reason.message : 'Advisory request failed.'} `
          + 'Some reads may have completed or incident records may have been persisted before the failure. Check Incidents & Alerts before retrying.',
      });
    } finally { requestLock.current = false; setRunning(false); }
  };

  const runKubernetesAdvisory = async (prompt: string) => {
    if (requestLock.current) return;
    requestLock.current = true;
    push({ role: 'user', text: prompt });
    setRunning(true);
    setTab('evidence');
    try {
      const observation = rec(await apiRequest<unknown>('/kubernetes/incidents'));
      if (
        !observation
        || observation.namespace !== DEMO_NAMESPACE
        || !Array.isArray(observation.incidents)
        || !rec(observation.persistence)
      ) {
        throw new Error('The backend returned an unexpected Kubernetes observation.');
      }
      let persistedIncidents: Rec[] = [];
      let discoveryStatus = 'unavailable';
      let discoveryError = '';
      try {
        const history = rec(await apiRequest<unknown>(
          '/incidents/history?source=operational',
        ));
        if (!history || !Array.isArray(history.incidents)) {
          throw new Error('The backend returned an unexpected incident history response.');
        }
        persistedIncidents = history.incidents
          .map(rec)
          .filter((item): item is Rec => item !== null)
          .filter(item =>
            item.service === 'kubernetes-lab'
            && typeof item.incident_id === 'string'
            && typeof item.fingerprint === 'string'
            && item.fingerprint.startsWith('k8s:kybernaut-lab:'),
          );
        discoveryStatus = 'completed';
      } catch (reason) {
        discoveryStatus = 'failed';
        discoveryError = reason instanceof Error
          ? reason.message
          : 'Persisted incident history could not be loaded.';
      }
      push({
        role: 'workflow',
        service: `Kubernetes · ${DEMO_NAMESPACE}`,
        result: {
          service: `Kubernetes · ${DEMO_NAMESPACE}`,
          investigation_type: 'kubernetes_observation',
          kubernetes: observation,
          kubernetes_status: 'completed',
          persisted_incidents: persistedIncidents,
          persistence_discovery_status: discoveryStatus,
          persistence_discovery_error: discoveryError,
        },
      });
      void refresh('incidents');
      toast(discoveryStatus === 'completed'
        ? 'Kubernetes observation and persisted history received.'
        : 'Observation completed; persisted incident history could not be confirmed.');
    } catch (reason) {
      push({
        role: 'agent', tone: 'error',
        text: `${reason instanceof Error ? reason.message : 'Kubernetes investigation failed.'} `
          + 'No remediation was started.',
      });
    } finally { requestLock.current = false; setRunning(false); }
  };

  const investigateKubernetesIncident = async (
    incidentId: string,
    preferredAction?: string,
    idempotencyKey?: string,
  ): Promise<Rec | null> => {
    if (requestLock.current) return null;
    requestLock.current = true;
    push({
      role: 'user',
      text: preferredAction
        ? `Execute "${preferredAction.replace(/_/g, ' ')}" for incident ${incidentId}.`
        : `Run RCA and policy evaluation for persisted Kubernetes incident ${incidentId}.`,
    });
    const stepsId = push({
      role: 'steps', title: 'Investigating…',
      steps: [
        { label: 'Running root-cause analysis with the language model', status: 'active' },
        { label: 'Checking the action against safety policy', status: 'pending' },
        { label: 'Creating the approval request', status: 'pending' },
      ],
    });
    setRunning(true);
    setTab('investigation');
    try {
      const query = preferredAction ? `?preferred_action=${encodeURIComponent(preferredAction)}` : '';
      const result = rec(await apiRequest<unknown>(
        `/incidents/${encodeURIComponent(incidentId)}/investigate${query}`,
        {
          method: 'POST',
          headers: {
            'Idempotency-Key': idempotencyKey
              ?? `console-k8s-${incidentId}-${preferredAction ?? 'primary'}`,
          },
        },
      ));
      if (!result || typeof result.status !== 'string') {
        throw new Error('The backend returned an unexpected investigation response.');
      }
      const rca = rec(result.rca);
      const decision = rec(result.policy_decision);
      const reasons = Array.isArray(decision?.reasons) ? decision!.reasons.join('; ') : '';
      updateMessage(stepsId, {
        title: 'Investigation complete',
        steps: [
          { label: 'Root-cause analysis', status: rca ? 'done' : 'error',
            detail: rca ? `Root cause: ${text(rca.root_cause)}`
              : `Not available (${text(result.rca_status, 'not reported')}${result.rca_error ? `: ${text(result.rca_error)}` : ''}). The deterministic plan is unaffected.` },
          { label: 'Safety policy', status: decision ? (decision.allowed ? 'done' : 'error') : 'skipped',
            detail: decision ? (decision.allowed ? 'Allowed. Human approval is required before anything runs.' : `Blocked: ${reasons || 'policy denied'}`)
              : `No action to check (${text(result.plan_note, 'no proposal')}).` },
          { label: 'Approval request', status: result.approval_id ? 'done' : 'skipped',
            detail: result.approval_id
              ? 'Approval is created; Execute approves this plan and continues remediation.'
              : 'No approval was created.' },
        ],
      });
      push({ role: 'remediation', incidentId, result });
      push({
        role: 'workflow',
        service: 'Kubernetes · ' + DEMO_NAMESPACE,
        result: { ...result, investigation_type: 'response_graph' },
      });
      await Promise.all([
        refresh('incidents'), refresh('approvals'), refresh('audit'),
      ]);
      return result;
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'Incident investigation failed.';
      updateMessage(stepsId, {
        title: 'Investigation could not be completed',
        steps: [
          { label: 'Root-cause analysis', status: 'error', detail: message },
          { label: 'Checking the action against safety policy', status: 'skipped' },
          { label: 'Creating the approval request', status: 'skipped' },
        ],
      });
      push({
        role: 'agent', tone: 'error',
        text: `${message} Nothing was executed.`,
      });
      return null;
    } finally { requestLock.current = false; setRunning(false); }
  };

  // Reads the stored record, checks live health, observes the cluster and reconciles a
  // confirmed-cleared condition. No remediation is executed and no approval is created here.
  // Progress is shown step by step in the conversation, then only the options that can
  // really be executed are offered inline.
  const startInvestigation = async () => {
    if (!selected || requestLock.current) return;
    const id = String(selected.incident_id);
    const target = String(selected.service);
    const checkHealth = (SERVICES as readonly string[]).includes(target);
    const selectedEvidence = rec(selected.source_evidence);
    const selectedIsLocalServiceIncident =
      selectedEvidence?.simulation === 'local_service_failure';
    const classificationLabel = 'Classifying the incident from available evidence';
    requestLock.current = true;
    setRunning(true);
    setTab('investigation');
    push({ role: 'user', text: `Investigate incident ${id}` });
    const recordDetail = `${text(selected.incident_type).replace(/_/g, ' ')} · ${text(selected.severity)} · ${text(selected.status)}`;
    const TITLE = 'Investigating…';
    const steps: Step[] = [
      { label: 'Reading the stored incident record', status: 'active' },
      { label: `Checking live health of ${target}`, status: 'pending' },
      { label: classificationLabel, status: 'pending' },
      { label: 'Checking runbook and safety policy', status: 'pending' },
      { label: 'Shortlisting actions I can run', status: 'pending' },
    ];
    const stepsId = push({ role: 'steps', title: TITLE, steps: steps.map(step => ({ ...step })) });
    // Every change is pushed to the conversation, so the operator watches the
    // investigation advance one step at a time.
    const mark = (index: number, patch: Partial<Step>, title = TITLE) => {
      steps[index] = { ...steps[index], ...patch };
      updateMessage(stepsId, { title, steps: steps.map(step => ({ ...step })) });
    };
    let optionsId: number | null = null;
    try {
      // Backend reads start immediately; the steps below reveal their results in order.
      const healthCall = checkHealth
        ? getServiceHealth(target).then(
          health => ({ kind: 'ok' as const, health }),
          reason => ({ kind: 'err' as const, reason }),
        )
        : null;
      const optionsCall = Promise.all([
        getRemediationOptions(id),
        getAgentStatus().catch(reason => ({
          remediation_mode: null,
          status_error: reason instanceof Error ? reason.message : 'Could not load remediation mode.',
        })),
      ]).then(
        value => ({ kind: 'ok' as const, value }),
        reason => ({ kind: 'err' as const, reason }),
      );

      // 1 · stored record
      await pause(800);
      mark(0, { status: 'done', detail: recordDetail });

      // 2 · live health
      if (healthCall) {
        mark(1, { status: 'active' });
        const outcome = await atLeast(healthCall, 1200);
        if (outcome.kind === 'ok') {
          const health = outcome.health;
          const verdict = health.health_status === 'healthy' && health.freshness_status === 'fresh'
            ? 'healthy' : String(health.health_status);
          mark(1, { status: 'done',
            detail: `Prometheus reports ${verdict} (freshness: ${text(health.freshness_status)}).` });
        } else {
          mark(1, { status: 'skipped',
            detail: `Live health unavailable: ${outcome.reason instanceof Error ? outcome.reason.message : 'request failed'}. Continuing with the stored record.` });
        }
      } else {
        mark(1, { status: 'skipped', detail: 'This record is not on one of the four application services.' });
      }

      // 3 · classification
      mark(2, { status: 'active' });
      const optionsOutcome = await atLeast(optionsCall, 1400);
      if (optionsOutcome.kind === 'err') throw optionsOutcome.reason;
      const [data, agentStatus] = optionsOutcome.value;
      setRemediationModes(current => ({
        ...current,
        [id]: {
          mode: typeof agentStatus.remediation_mode === 'string'
            ? agentStatus.remediation_mode
            : null,
          error: typeof agentStatus.status_error === 'string'
            ? agentStatus.status_error : null,
        },
      }));
      const options = (Array.isArray(data.options) ? data.options : []).map(rec).filter((item): item is Rec => item !== null);
      const executableAll = options.filter(item => item.kind === 'executable');
      const runnable = executableAll.filter(optionExecutionAvailable).length;
      const blockedCount = executableAll.length - runnable;
      const localServiceIncident =
        selectedIsLocalServiceIncident || data.source === 'local_service_simulator';
      if (data.condition_cleared === true) {
        await Promise.all([refresh('incidents'), refresh('audit')]);
        loaded.reload();
        setTimelineReload(n => n + 1);
      }
      mark(2, {
        label: classificationLabel,
        status: data.live_evidence || localServiceIncident ? 'done' : 'skipped',
        detail: localServiceIncident
          ? `Classified as ${text(data.incident_type).replace(/_/g, ' ')} for ${target} from incident evidence.`
          : data.live_evidence
            ? `Classified as ${text(data.incident_type).replace(/_/g, ' ')} from current evidence.`
            : text(data.note, 'The incident record is used because current evidence is unavailable.'),
      });

      // 4 · runbook and safety policy
      mark(3, { status: 'active' });
      await pause(1200);
      mark(3, { status: 'done',
        detail: `${executableAll.length} candidate action${executableAll.length === 1 ? '' : 's'}: ${runnable} passed${blockedCount ? `, ${blockedCount} blocked` : ''}.` });

      // 5 · shortlist. The Execute card appears now, while this step is still running,
      // and unlocks the moment the investigation completes.
      mark(4, { status: 'active' });
      if (runnable > 0) optionsId = push({ role: 'options', incidentId: id, data, analysing: true });
      await pause(1100);
      mark(4, { status: 'done',
        detail: runnable
          ? `${runnable} action${runnable === 1 ? '' : 's'} can be executed with your approval.`
          : 'Nothing can be executed automatically right now.' }, 'Investigation complete');
      if (optionsId !== null) updateMessage(optionsId, { analysing: false });
      else push({ role: 'options', incidentId: id, data });
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : 'The investigation request failed.';
      const activeAt = steps.findIndex(step => step.status === 'active');
      const failedAt = activeAt >= 0 ? activeAt : 2;
      steps.forEach((step, index) => {
        if (index === failedAt) steps[index] = { ...step, status: 'error', detail: message };
        else if (step.status === 'pending' || step.status === 'active') steps[index] = { ...step, status: 'skipped' };
      });
      updateMessage(stepsId, { title: 'Investigation could not be completed', steps: steps.map(step => ({ ...step })) });
      if (optionsId !== null) updateMessage(optionsId, { analysing: false });
    } finally { requestLock.current = false; setRunning(false); }
  };

  // Kubernetes actions use their persisted approval workflow. Simulator and
  // payment Compose actions use dedicated permission-checked endpoints.
  const executeOption = async (option: Rec) => {
    if (!selected || typeof selected.incident_id !== 'string' ||
      typeof option.action !== 'string' ||
      typeof option.resource !== 'string' || requestLock.current ||
      executeLock.current) return;
    executeLock.current = true;
    setRunning(true);
    try {
      if (option.action === 'migrate_payment_history_and_deploy') {
        const confirmed = window.confirm(
          'Before continuing, pause all payment writes, including direct callers to payment-service. Keep them paused through migration and verification. Approve migrating payment history and rebuilding/replacing only payment-service?',
        );
        if (!confirmed) return;
        const svc = 'payment-service';
        const title = text(option.title, 'Migrate payment history and deploy the PostgreSQL-backed fix');
        const steps: Step[] = [
          { label: 'Approval', status: 'active', detail: 'Recording the approval and confirmed payment-write pause.' },
          { label: 'Migration and deployment', status: 'pending', detail: title },
          { label: 'Verification', status: 'pending', detail: 'Checking PostgreSQL-backed health and migrated payment history.' },
          { label: 'Incident status', status: 'pending', detail: 'Fresh memory telemetry is still required to resolve the incident.' },
        ];
        const stepsId = push({ role: 'steps', title: 'Payment remediation progress', steps });
        const updateStep = (index: number, patch: Partial<Step>) => {
          steps[index] = { ...steps[index], ...patch };
          updateMessage(stepsId, { steps: [...steps] });
        };
        try {
          updateStep(0, { status: 'done', detail: 'You approved the scoped action and confirmed all payment writes are paused.' });
          updateStep(1, { status: 'active' });
          const result = await executePaymentComposeRemediation(selected.incident_id);
          updateStep(1, { status: 'done', detail: `Migrated ${text(result.source_payment_count, 'the')} payment records and replaced only ${svc}.` });
          updateStep(2, { status: result.health_verified === true && result.history_verified === true ? 'done' : 'error',
            detail: result.health_verified === true && result.history_verified === true
              ? 'PostgreSQL-backed health and payment history were verified.'
              : 'The API did not confirm both health and payment history.' });
          updateStep(3, { status: 'done', detail: 'The incident remains open until fresh memory telemetry confirms recovery.' });
          await Promise.all([refresh('incidents'), refresh('audit')]);
          loaded.reload();
          setTimelineReload(n => n + 1);
          push({
            role: 'report',
            report: {
              title: 'Payment fix deployed · recovery not yet verified',
              status: 'not_resolved',
              summary: text(result.note, 'The PostgreSQL-backed payment fix was deployed and verified. Fresh memory telemetry is still required before resolving this incident.'),
              rows: [
                ['Incident', String(selected.incident_id)],
                ['Service', svc],
                ['Source records', text(result.source_payment_count)],
                ['Database records', text(result.database_payment_count)],
                ['Health and history', 'Verified'],
                ['Audit', result.audit_recorded === false ? `Not recorded (${text(result.audit_error, 'unknown error')})` : 'Recorded'],
                ['Incident', 'Still open pending fresh memory telemetry'],
              ],
              next: ['Keep payment writes paused until you confirm the replacement is healthy, then resume traffic and review fresh memory telemetry.'],
            },
          });
        } catch (failure) {
          updateStep(steps.findIndex(step => step.status === 'active'), {
            status: 'error',
            detail: failure instanceof Error ? failure.message : 'The payment remediation failed.',
          });
          throw failure;
        }
        return;
      }
      if (option.action === 'clear_local_simulation') {
        const svc = String(selected.service);
        const remediationTitle = text(
          option.title,
          `Restore normal operation on ${svc}`,
        );
        const startedMs = Date.now();
        const TITLE = 'Incident resolution progress';
        const steps: Step[] = [
          { label: 'Investigated', status: 'active',
            detail: `Reviewing ${text(selected.incident_type).replace(/_/g, ' ')} for ${svc} and its scoped remediation.` },
          { label: 'Approval', status: 'pending',
            detail: 'Recording your approval and checking your permission and the incident state.' },
          { label: 'Executed', status: 'pending', detail: remediationTitle },
          { label: 'Verified', status: 'pending',
            detail: 'Waiting for confirmation that the incident condition is inactive.' },
          { label: 'Resolved', status: 'pending',
            detail: 'Closing the incident and writing the audit event.' },
        ];
        const stepsId = push({ role: 'steps', title: TITLE, steps: steps.map(step => ({ ...step })) });
        const mark = (index: number, patch: Partial<Step>, title = TITLE) => {
          steps[index] = { ...steps[index], ...patch };
          updateMessage(stepsId, { title, steps: steps.map(step => ({ ...step })) });
        };
        await pause(900);
        mark(0, { status: 'done',
          detail: `Reviewed ${text(selected.incident_type).replace(/_/g, ' ')} for ${svc} and its scoped remediation.` });
        mark(1, { status: 'active' });
        await pause(1100);
        mark(1, { status: 'done',
          detail: 'You approved this action by selecting Execute; the API checked your permission and the incident state.' });
        mark(2, { status: 'active' });
        let result: Rec;
        try {
          result = await atLeast(executeLocalSimulation(selected.incident_id), 1700);
        } catch (failure) {
          mark(2, { status: 'error',
            detail: failure instanceof Error ? failure.message : 'The request failed.' }, 'Execution failed');
          mark(3, { status: 'skipped', detail: 'The backend did not confirm successful resolution.' }, 'Execution failed');
          mark(4, { status: 'skipped', detail: 'The incident was not resolved.' }, 'Execution failed');
          throw failure;
        }
        const resolvedOk = result.resolved === true;
        const auditOk = result.audit_recorded !== false;
        mark(2, { status: 'done', detail: `Completed: ${remediationTitle}.` });
        mark(3, { status: 'active' });
        await pause(1300);
        mark(3, { status: resolvedOk ? 'done' : 'error',
          detail: resolvedOk
            ? 'The service confirmed the incident condition is inactive and the incident is resolved.'
            : 'The condition was cleared without confirmed incident resolution.' });
        mark(4, { status: 'active' });
        await pause(1000);
        mark(4, resolvedOk
          ? { status: 'done', detail: auditOk ? 'Incident marked RESOLVED and the audit event was recorded.' : 'Incident marked RESOLVED; the audit event could not be recorded.' }
          : { status: 'skipped', detail: 'The incident stays open because resolution was not confirmed.' });
        await Promise.all([
          refresh('incidents'), refresh('services'), refresh('audit'),
        ]);
        loaded.reload();
        setTimelineReload(n => n + 1);
        await pause(600);
        const seconds = Math.max(0.1, (Date.now() - startedMs) / 1000).toFixed(1);
        push({
          role: 'report',
          report: {
            title: resolvedOk ? 'Incident resolved' : 'Remediation not confirmed',
            status: resolvedOk ? 'resolved' : 'not_resolved',
            summary: resolvedOk
              ? `${remediationTitle} completed, and the incident is now resolved.`
              : `${remediationTitle} completed, but resolution was not confirmed, so the incident stays open.`,
            rows: [
              ['Incident', String(selected.incident_id)],
              ['Service', svc],
              ['Action', remediationTitle],
              ['Approval', 'You approved this action with Execute'],
              ['Verification', resolvedOk ? 'Condition inactive · incident RESOLVED' : 'Not confirmed'],
              ['Audit', auditOk ? 'Event recorded' : `Not recorded (${text(result.audit_error, 'unknown error')})`],
              ['Took', `${seconds}s`],
            ],
            next: auditOk ? undefined
              : ['Audit recording failed; notify the application owner.'],
          },
        });
        return;
      }

      const { approvals } = await getApprovals();
      const matchingApprovals = approvals
        .map(rec)
        .filter((approval): approval is Rec => approval !== null)
        .filter(approval => {
          const proposal = rec(approval.proposed_action);
          return typeof approval.approval_id === 'string' &&
            approval.incident_ref === selected.incident_id &&
            String(approval.status).toUpperCase() === 'PENDING' &&
            proposal?.action === option.action &&
            proposal.resource === option.resource &&
            (typeof option.target_replicas !== 'number' ||
              proposal.target_replicas === option.target_replicas);
        });

      if (matchingApprovals.length > 1) {
        push({
          role: 'agent',
          tone: 'error',
          text: 'More than one pending approval matches this incident and action. No remediation was run; review the approvals before retrying.',
        });
        return;
      }

      if (matchingApprovals.length === 1) {
        const approval = matchingApprovals[0];
        push({
          role: 'agent',
          text: 'Using the existing pending approval for this incident and action.',
        });
        await executeApproval(
          String(approval.approval_id),
          'Approved from the Console Execute action.',
          approval,
        );
        return;
      }

      const result = await investigateKubernetesIncident(
        String(selected.incident_id),
        option.action,
        `console-execute-${crypto.randomUUID()}`,
      );
      const approvalId = result?.approval_id;
      if (!result || typeof approvalId !== 'string') {
        if (result && result.status !== 'resolved') {
          const decision = rec(result.policy_decision);
          const reasons = Array.isArray(decision?.reasons)
            ? decision.reasons.filter((reason): reason is string => typeof reason === 'string')
            : [];
          push({
            role: 'agent',
            tone: 'error',
            text: decision?.allowed === false
              ? `Policy blocked this action: ${reasons.join('; ') || 'a safety check failed'}. No remediation was run.`
              : 'The workflow did not create an executable approval. No remediation was run.',
          });
        }
        return;
      }

      await executeApproval(
        approvalId,
        'Approved from the Console Execute action.',
      );
    } catch (failure) {
      push({
        role: 'agent',
        tone: 'error',
        text: `${failure instanceof Error ? failure.message : 'Execution failed.'} The incident was not reported as resolved.`,
      });
    } finally {
      executeLock.current = false;
      setRunning(false);
    }
  };

  // The backend enforces permission, approval state, fresh evidence, and policy.
  const executeApproval = async (
    approvalId: string,
    reason: string,
    approvalOverride?: Rec,
  ) => {
    const approval = approvalOverride ||
      (store.approvals.data || []).find(item => item.approval_id === approvalId);
    const proposed = rec(approval?.proposed_action);
    const actionLabel = text(proposed?.action, 'remediation').replace(/_/g, ' ');
    const resourceLabel = text(proposed?.resource, '');
    const ref = typeof approval?.incident_ref === 'string' ? approval.incident_ref : null;
    const startedMs = Date.now();
    const stepsId = push({
      role: 'steps', title: 'Executing…',
      steps: [
        { label: 'Recording your approval', status: 'active' },
        { label: `Running ${actionLabel}${resourceLabel ? ` on ${resourceLabel}` : ''}`, status: 'pending' },
        { label: 'Verifying recovery', status: 'pending' },
      ],
    });
    try {
      // The backend call starts now; the steps below reveal its result one at a time.
      const call = decide(approvalId, 'approved', reason).then(
        value => ({ kind: 'ok' as const, value }),
        error => ({ kind: 'err' as const, error }),
      );
      await pause(900);
      const settled = await call;
      if (settled.kind === 'err') throw settled.error;
      const outcome = settled.value;
      const out = rec(outcome);
      const execution = rec(out?.execution);
      const verification = rec(out?.verification);
      const status = text(out?.status, '');
      const live = execution?.mode === 'live' && execution?.executed === true && execution?.ok === true;
      const dry = execution?.mode === 'dry_run' || status === 'dry_run_complete';
      const recovered = verification?.recovered === true;
      // Surface WHY a workflow stopped (policy/runbook/execution), never just "blocked".
      const policyReasons = rec(out?.policy_decision)?.reasons;
      const blockedBy = [
        ...(Array.isArray(policyReasons) ? policyReasons.map(String) : []),
        text(rec(out?.runbook_resolution)?.reason, ''),
        text(execution?.blocked_reason, ''),
        text(execution?.error, ''),
      ].filter(Boolean);
      const blockedText = blockedBy.length ? ` Reason: ${[...new Set(blockedBy)].join('; ')}.` : '';
      const verified = status === 'resolved' && live && recovered;
      const incidentKey = typeof selected?.incident_id === 'string'
        ? selected.incident_id : ref;
      const executionOutcome = dry
        ? {
          status: 'dry_run' as const,
          detail: 'Dry run: the approved command was recorded but not executed. The incident remains open.',
        }
        : verified
          ? {
            status: 'resolved' as const,
            detail: 'Live remediation completed and recovery was verified.',
          }
          : {
            status: 'not_resolved' as const,
            detail: execution?.ok === false
              ? 'Remediation did not complete successfully. The incident remains open.'
              : 'Recovery was not verified. The incident remains open.',
          };
      if (incidentKey) {
        setExecutionOutcomes(current => ({ ...current, [incidentKey]: executionOutcome }));
        if (verified) {
          setResolvedIds(ids => (ids.includes(incidentKey) ? ids : [...ids, incidentKey]));
        }
      }
      const approvalStep: Step = { label: 'Approval recorded', status: 'done', detail: `Approved with your reason: ${reason}` };
      const runStep: Step = { label: live ? `Executed ${actionLabel}${resourceLabel ? ` on ${resourceLabel}` : ''}` : dry ? 'Dry run: the command was recorded but not run' : `Execution of ${actionLabel}`,
        status: live ? 'done' : dry ? 'skipped' : execution ? (execution.ok === false ? 'error' : 'done') : 'skipped',
        detail: dry ? 'The API is running in dry_run, so nothing in the cluster changed. Restart it with ./scripts/dev_live.sh to execute for real.' : execution?.ok === false ? `The command did not succeed.${blockedText}` : blockedText || undefined };
      const verifyStep: Step = { label: 'Recovery verification', status: verification ? (recovered ? 'done' : 'error') : 'skipped',
        detail: verification ? text(verification.reason, recovered ? 'Recovered' : 'Not recovered') : 'No verification was run.' };
      // Reveal: approval -> running the action -> verifying recovery -> final state.
      updateMessage(stepsId, { title: 'Executing…', steps: [
        approvalStep,
        { label: `Running ${actionLabel}${resourceLabel ? ` on ${resourceLabel}` : ''}`, status: 'active' },
        { label: 'Verifying recovery', status: 'pending' },
      ] });
      await pause(1400);
      updateMessage(stepsId, { title: 'Executing…', steps: [
        approvalStep, runStep, { label: 'Verifying recovery', status: 'active' },
      ] });
      await pause(1200);
      updateMessage(stepsId, {
        title: verified ? 'Resolved' : dry ? 'Dry run complete' : 'Execution finished',
        steps: [approvalStep, runStep, verifyStep],
      });
      await pause(600);
      const seconds = Math.max(0.1, (Date.now() - startedMs) / 1000).toFixed(1);
      push({
        role: 'report',
        report: {
          title: verified ? 'Incident resolved' : dry ? 'Dry run complete' : 'Remediation outcome',
          status: verified ? 'resolved' : dry ? 'dry_run' : 'not_resolved',
          summary: verified
            ? `The ${actionLabel} was executed and recovery was verified from fresh cluster data. The incident is resolved.`
            : dry
              ? `The ${actionLabel} was approved and recorded, but the API is running in dry_run, so no change was made and the incident is not resolved. Restart the API with ./scripts/dev_live.sh to execute for real.`
              : `The approval was recorded. Workflow status: ${status || 'not reported'}.${blockedText} ${verification ? `Verification: ${text(verification.reason, 'not reported')}.` : 'Recovery was not verified, so the incident is not marked resolved.'}`,
          rows: [
            ['Incident', incidentKey || '—'],
            ['Action', `${actionLabel}${resourceLabel ? ` on ${resourceLabel}` : ''}`],
            ['Approval', `${approvalId} · approved by you`],
            ['Execution', live ? 'Executed live' : dry ? 'Dry run (not executed)' : execution ? (execution.ok === false ? 'Failed' : 'Completed') : 'Not run'],
            ['Verification', verification ? text(verification.reason, recovered ? 'Recovered' : 'Not recovered') : 'Not run'],
            ['Incident state', verified ? 'RESOLVED' : 'Still OPEN'],
            ['Took', `${seconds}s`],
          ],
          next: verified ? undefined
            : dry ? ['Switch the API to live mode, then run the action again.']
              : ['Review the audit trail, then retry or follow the manual guidance in the Remediation tab.'],
        },
      });
      await Promise.all([refresh('incidents'), refresh('approvals'), refresh('audit')]);
      loaded.reload();
      setTimelineReload(n => n + 1);
    } catch (failure) {
      updateMessage(stepsId, {
        title: 'Execution was not started',
        steps: [
          { label: 'Recording your approval', status: 'error', detail: failure instanceof Error ? failure.message : 'The request failed.' },
          { label: `Running ${actionLabel}`, status: 'skipped' },
          { label: 'Verifying recovery', status: 'skipped' },
        ],
      });
      throw failure;
    }
  };

  const openIncident = (id: string) => nav(`/console?incident=${encodeURIComponent(id)}`);

  const runReadQuery = async (
    prompt: string,
    title: string,
    request: () => Promise<unknown>,
    summarize: (data: unknown) => string,
    layout?: AnswerLayout,
  ) => {
    if (requestLock.current) return;
    requestLock.current = true;
    push({ role: 'user', text: prompt });
    setRunning(true);
    setTab('investigation');
    try {
      const data = await request();
      push({ role: 'answer', title, summary: summarize(data), data, layout });
    } catch (reason) {
      push({
        role: 'agent',
        tone: 'error',
        text: reason instanceof Error ? reason.message : `${title} request failed.`,
      });
    } finally {
      requestLock.current = false;
      setRunning(false);
    }
  };

  const runHealthQuery = (prompt: string, target?: string) =>
    runReadQuery(
      prompt,
      'Live service health',
      async () => {
        const configured = (await getConfiguredServices()).services;
        if (target && !configured.includes(target)) {
          throw new Error(
            `Unsupported service "${target}". Configured services: ${configured.join(', ')}.`,
          );
        }
        const services = target ? [target] : configured;
        const results = await Promise.all(services.map(async name => {
          try {
            return await getServiceHealth(name);
          } catch (reason) {
            return {
              service: name,
              health_status: 'unknown',
              evidence_status: 'unavailable',
              error: reason instanceof Error ? reason.message : 'Diagnosis request failed.',
            };
          }
        }));
        return { source: 'Prometheus diagnosis', checked_at: new Date().toISOString(), services: results };
      },
      data => {
        const result = rec(data);
        const services = Array.isArray(result?.services)
          ? result.services.map(rec).filter((item): item is Rec => item !== null)
          : [];
        return `Live health checks completed for ${services.length} service${services.length === 1 ? '' : 's'}.`;
      },
      'health',
    );

  // Explains the health of ONE service inside the conversation (no page change).
  // When an incident is open on that service its facts are shown beside the metrics.
  const runServiceHealth = (prompt: string, target: string) =>
    runReadQuery(
      prompt,
      `Service health · ${target}`,
      async (): Promise<ServiceHealthData> => {
        const configured = (await getConfiguredServices()).services;
        if (!configured.includes(target)) {
          throw new Error(`Unsupported service "${target}". Configured services: ${configured.join(', ')}.`);
        }
        const [healthResult, telemetryResult] = await Promise.allSettled([
          getServiceHealth(target), getLiveTelemetry(),
        ]);
        const telemetry = telemetryResult.status === 'fulfilled'
          ? rec(rec(telemetryResult.value.services)?.[target]) : null;
        const onService = openIncidentsOf(store.incidents.data || [])
          .filter(item => item.service === target);
        const incident = selected && selected.service === target ? selected : onService[0] ?? null;
        return {
          service: target,
          health: healthResult.status === 'fulfilled' ? healthResult.value : null,
          healthError: healthResult.status === 'rejected'
            ? (healthResult.reason instanceof Error ? healthResult.reason.message : 'request failed') : undefined,
          telemetry,
          incident,
          otherOpen: onService.filter(item => item.incident_id !== incident?.incident_id),
        };
      },
      data => {
        const result = rec(data);
        const health = rec(result?.health);
        return health
          ? `${target}: ${text(health.health_status)} (freshness ${text(health.freshness_status)}).`
          : `Live health for ${target} could not be read.`;
      },
      'service_health',
    );

  const sortNewestFirst = (events: Rec[]) =>
    events.map((event, index) => ({
      event,
      index,
      timestamp: typeof event.timestamp === 'string'
        ? Date.parse(event.timestamp)
        : Number.NaN,
    })).sort((left, right) => {
      const leftValid = Number.isFinite(left.timestamp);
      const rightValid = Number.isFinite(right.timestamp);
      if (leftValid !== rightValid) return leftValid ? -1 : 1;
      if (leftValid && left.timestamp !== right.timestamp) {
        return right.timestamp - left.timestamp;
      }
      return left.index - right.index;
    }).map(item => item.event);

  const sortChronologically = (events: Rec[]) =>
    events.map((event, index) => ({
      event,
      index,
      timestamp: typeof event.timestamp === 'string'
        ? Date.parse(event.timestamp)
        : Number.NaN,
    })).sort((left, right) => {
      const leftValid = Number.isFinite(left.timestamp);
      const rightValid = Number.isFinite(right.timestamp);
      if (leftValid !== rightValid) return leftValid ? -1 : 1;
      if (leftValid && left.timestamp !== right.timestamp) {
        return left.timestamp - right.timestamp;
      }
      return left.index - right.index;
    }).map(item => item.event);

  const runIncidentTimeline = (prompt: string, requestedId: string) =>
    runReadQuery(
      prompt,
      'Incident timeline',
      async () => {
        await getIncident(requestedId);
        try {
          const response = rec(await apiRequest<unknown>(
            `/incidents/${encodeURIComponent(requestedId)}/timeline`,
          ));
          if (!response || !Array.isArray(response.events)) {
            throw new Error('The backend returned an unexpected incident timeline response.');
          }
          const events = response.events.map(rec)
            .filter((event): event is Rec => event !== null);
          return {
            incident_id: requestedId,
            events: sortChronologically(events),
          };
        } catch (reason) {
          if (reason instanceof ApiError && reason.status === 404) {
            return { incident_id: requestedId, events: [] };
          }
          throw reason;
        }
      },
      data => {
        const result = rec(data);
        const events = Array.isArray(result?.events) ? result.events : [];
        return events.length
          ? `${events.length} recorded timeline event${events.length === 1 ? '' : 's'} for ${text(result?.incident_id)}, oldest first.`
          : `No timeline events were found for incident ${text(result?.incident_id)}.`;
      },
      'timeline',
    );

  const runIncidentLookup = (prompt: string, requestedId: string) =>
    runReadQuery(
      prompt,
      'Persisted incident lookup',
      async () => {
        const incident = await getIncident(requestedId);
        const timeline = await getAuditEvents(requestedId, 1000)
          .then(result => ({ status: 'available', events: result.events }))
          .catch(reason => ({
            status: 'unavailable',
            error: reason instanceof Error ? reason.message : 'Audit query failed.',
            events: [],
          }));
        let investigation: unknown = null;
        if (/\b(investigate|run rca|analy[sz]e root cause)\b/i.test(prompt)) {
          const sourceEvidence = rec(incident.source_evidence);
          const status = String(incident.status || '').toUpperCase();
          let ineligibleReason: string | null = null;
          if (isDemo(incident)) {
            ineligibleReason = 'This is simulated demo evidence; operational Kubernetes investigation is not available.';
          } else if (incident.service !== 'kubernetes-lab') {
            ineligibleReason = `This incident belongs to ${text(incident.service)}, not the supported Kubernetes lab service.`;
          } else if (!['OPEN', 'REMEDIATING'].includes(status)) {
            ineligibleReason = `The incident status is ${text(incident.status)}, so its occurrence is no longer active.`;
          } else if (sourceEvidence?.source !== 'kubernetes') {
            ineligibleReason = 'The stored evidence is not a Kubernetes observation.';
          } else if (sourceEvidence.namespace !== DEMO_NAMESPACE) {
            ineligibleReason = `The stored Kubernetes evidence is outside the supported ${DEMO_NAMESPACE} namespace.`;
          } else if (
            typeof incident.source_timestamp !== 'string'
            || sourceEvidence.source_timestamp !== incident.source_timestamp
          ) {
            ineligibleReason = 'The incident does not have matching, timestamped Kubernetes evidence.';
          }

          if (ineligibleReason) {
            investigation = { status: 'not_started', reason: ineligibleReason };
          } else {
            push({ role: 'agent', text: `Submitting the authorized investigation request for ${requestedId}. The backend will revalidate the active occurrence and evidence.` });
            try {
              const response = rec(await apiRequest<unknown>(
                `/incidents/${encodeURIComponent(requestedId)}/investigate`,
                {
                  method: 'POST',
                  headers: { 'Idempotency-Key': `console-k8s-${requestedId}` },
                },
              ));
              if (!response) {
                throw new Error('The investigation API returned an invalid response.');
              }
              investigation = response;
              loaded.reload();
            } catch (reason) {
              const statusCode = reason instanceof ApiError ? reason.status : undefined;
              const detail = statusCode === 400
                ? 'The backend rejected the workload because it is outside the supported Kubernetes lab allowlist.'
                : statusCode === 403
                  ? 'The backend denied this investigation request.'
                  : statusCode === 409
                    ? 'The backend rejected the investigation because the occurrence or its evidence is no longer eligible.'
                    : reason instanceof Error
                      ? reason.message
                      : 'The investigation request failed.';
              investigation = { status: 'failed', reason: detail };
            }
            await Promise.all([
              refresh('incidents'), refresh('approvals'), refresh('audit'),
            ]);
          }
        }
        return { incident, timeline, investigation };
      },
      data => {
        const result = rec(data);
        const incident = rec(result?.incident);
        const diagnosis = rec(incident?.diagnosis);
        const recordedPolicy = rec(diagnosis?.policy_recommendation);
        const investigation = rec(result?.investigation);
        return incident
          ? `${isDemo(incident) ? 'SIMULATED DEMO — no real workload was affected. ' : ''}`
            + `${text(incident.incident_id)} · ${text(incident.incident_type)} · ${text(incident.status)} · ${text(incident.severity)}. `
            + `Recorded diagnosis/RCA: ${text(diagnosis?.root_cause_analysis_status || incident.diagnosis_status, 'not recorded')}. `
            + `Policy outcome: ${text(recordedPolicy?.action, 'not recorded')}. `
            + (investigation
              ? `Investigation: ${text(investigation.status, 'response status not reported')}${investigation.reason ? ` — ${text(investigation.reason)}` : ''}.`
              : 'No investigation was rerun.')
          : 'The incident lookup returned no record.';
      },
    );

  // Free-form questions are answered by the language model from stored records only.
  const askAssistant = async (raw: string) => {
    if (requestLock.current) return;
    requestLock.current = true;
    push({ role: 'user', text: raw });
    const stepsId = push({
      role: 'steps', title: 'Looking through the records…',
      steps: [
        { label: 'Gathering the incident, audit trail, approvals and service health', status: 'active' },
        { label: 'Writing an answer strictly from those records', status: 'pending' },
      ],
    });
    setRunning(true);
    try {
      const result = await askConsole({ question: raw, incident_id: incidentId, service: String(selected?.service || '') || null });
      updateMessage(stepsId, {
        title: 'Answer ready',
        steps: [
          { label: 'Gathered records', status: 'done', detail: `Used: ${(Array.isArray(result.context_keys) ? result.context_keys : []).join(', ') || 'none'}` },
          { label: result.llm_used ? 'Answer written by the language model and validated against the records' : 'Answer built directly from the records',
            status: 'done' },
        ],
      });
      push({ role: 'assist', result });
    } catch (reason) {
      updateMessage(stepsId, {
        title: 'Could not answer',
        steps: [
          { label: 'Gathering the records', status: 'error', detail: reason instanceof Error ? reason.message : 'The request failed.' },
          { label: 'Writing an answer', status: 'skipped' },
        ],
      });
    } finally { requestLock.current = false; setRunning(false); }
  };

  const submit = () => {
    const raw = draft.trim();
    if (!raw || requestLock.current || running) return;
    setDraft('');
    if (pendingSafeFollowUp.current && /^yes\b/i.test(raw)) {
      const target = pendingSafeFollowUp.current;
      if (/\b(policy|recommendation|recommended)\b/i.test(raw)) {
        pendingSafeFollowUp.current = null;
        void runReadQuery(
          raw,
          'Current deterministic policy recommendation',
          async () => apiRequest<unknown>(`/decide/${encodeURIComponent(target)}`),
          data => {
            const result = rec(data);
            const decision = rec(result?.decision);
            return decision
              ? `${text(result?.service)} · ${text(decision.action)} · ${text(decision.reason)} Automatic execution: ${text(decision.automatic_execution, 'not reported')}. No action was executed.`
              : 'No policy decision was returned. No action was executed.';
          },
        );
        return;
      }
      if (/\b(health|healthy|status)\b/i.test(raw)) {
        pendingSafeFollowUp.current = null;
        void runHealthQuery(raw, target);
        return;
      }
      push({ role: 'user', text: raw });
      push({
        role: 'agent',
        text: `Which safe read-only option do you want for ${target}: the policy recommendation or current service health? A generic confirmation never executes remediation.`,
      });
      return;
    }
    const intent = classifyConsoleIntent(raw);
    // While an incident is open, follow-ups are about THAT incident and its service.
    const contextService = selected && (SERVICES as readonly string[]).includes(String(selected.service))
      ? String(selected.service) : undefined;
    const READ_KINDS = new Set(['audit', 'timeline', 'health', 'health_all', 'metrics', 'highest_p95', 'service_list', 'capabilities']);
    const explain = /\b(resolved?|fixed|solved|summar\w+|explain|why|what happened|root cause|report|what is|how|caus(?:e|es|ed|ing))\b/i;
    if (canRun && !READ_KINDS.has(intent.kind) && (
      intent.kind === 'ambiguous' || (selected && explain.test(raw)) ||
      (intent.kind === 'unsupported' && !/\b(remediat\w*|restart|stop|scale|delete|kill|fix|repair)\b/i.test(raw))
    )) {
      void askAssistant(raw);
      return;
    }
    if (!canRun && intent.kind !== 'capabilities' && intent.kind !== 'unsupported' && intent.kind !== 'ambiguous') {
      push({ role: 'user', text: raw });
      push({ role: 'agent', tone: 'error', text: 'Your account does not have read permission to query operational data.' });
    } else if (intent.kind === 'capabilities') {
      push({ role: 'user', text: raw });
      push({
        role: 'answer',
        title: 'Kybernaut Console capabilities',
        summary: 'I can query live Prometheus-backed service health and telemetry, inspect Kubernetes observations in kybernaut-lab, read operational incidents and their audit history, and show recorded recommendations. Read-only queries do not execute remediation; remediation requires the separate authorized approval workflow.',
      });
    } else if (intent.kind === 'health' || intent.kind === 'health_all') {
      const named = intent.service && !/^(it|this|that|there|incident|alert|the)$/i.test(intent.service)
        ? intent.service : undefined;
      const healthTarget = intent.kind === 'health_all' ? undefined : named || contextService;
      if (healthTarget) void runServiceHealth(raw, healthTarget);
      else void runHealthQuery(raw, undefined);
    } else if (intent.kind === 'service_list') {
      void runReadQuery(
        raw,
        'Configured services',
        getConfiguredServices,
        data => {
          const result = rec(data);
          const services = Array.isArray(result?.services) ? result.services : [];
          return `${services.length} configured service${services.length === 1 ? '' : 's'}. No health checks were run.`;
        },
        'services',
      );
    } else if (intent.kind === 'metrics' || intent.kind === 'highest_p95') {
      void runReadQuery(
        raw,
        'Live Prometheus telemetry',
        async () => {
          return selectTelemetryServices(
            await getLiveTelemetry(),
            intent.kind === 'metrics' ? intent.service || contextService : intent.service,
          );
        },
        data => {
          const result = rec(data);
          if (intent.kind === 'highest_p95' && result) {
            const services = rec(result.services);
            if (services) {
              return summarizeHighestP95({
                services,
                unavailable_services: Array.isArray(result.unavailable_services)
                  ? result.unavailable_services
                  : [],
              });
            }
          }
          const services = rec(result?.services) || {};
          return `Prometheus read timestamp: ${text(result?.observed_at)}. Missing or incomplete rows remain unavailable, not healthy.`;
        },
        'telemetry',
      );
    } else if (intent.kind === 'kubernetes') {
      const namespace = raw.match(/\bnamespace\s+([a-z0-9][a-z0-9-]*)\b/i)?.[1]?.toLowerCase();
      if (namespace && namespace !== DEMO_NAMESPACE) {
        push({ role: 'user', text: raw });
        push({
          role: 'agent',
          tone: 'error',
          text: `Unsupported namespace. Read-only Kubernetes investigations are limited to ${DEMO_NAMESPACE}.`,
        });
      } else {
        void runKubernetesAdvisory(raw);
      }
    } else if (intent.kind === 'incident' && (intent.incidentId || incidentId)) {
      void runIncidentLookup(raw, intent.incidentId || incidentId!);
    } else if (intent.kind === 'incident_list') {
      void runReadQuery(
        raw,
        'Latest operational incidents',
        async () => {
          const history = rec(await apiRequest<unknown>(
            '/incidents/history?source=operational',
          ));
          if (!history || !Array.isArray(history.incidents)) {
            throw new Error('The backend returned an unexpected incident history response.');
          }
          return history.incidents.map(rec)
            .filter((item): item is Rec => item !== null)
            .filter(item => item.source !== 'demo' && !isDemo(item));
        },
        data => {
          const rows = Array.isArray(data) ? data.map(rec)
            .filter((item): item is Rec => item !== null) : [];
          return rows.length
            ? rows.map(item => `${text(item.incident_id)} · ${text(item.incident_type)} · ${text(item.service)} · ${text(item.severity)} · ${text(item.status)} · ${text(item.opened_at)}`).join('\n')
            : 'No operational incidents are currently recorded.';
        },
      );
    } else if (intent.kind === 'audit') {
      const auditId = intent.incidentId || incidentId;
      const requestedCount = intent.count ?? 5;
      if (!Number.isSafeInteger(requestedCount) || requestedCount < 1 || requestedCount > 1000) {
        push({ role: 'user', text: raw });
        push({
          role: 'agent',
          tone: 'error',
          text: 'Audit history requests must specify between 1 and 1,000 entries.',
        });
        return;
      }
      void runReadQuery(
        raw,
        'Audit history',
        async () => {
          let events: Rec[];
          let unassociatedEventCount = 0;
          if (auditId) {
            const source = auditId.toLowerCase().startsWith('demo-')
              ? 'demo'
              : 'operational';
            const result = await getAuditEvents(auditId, requestedCount, source);
            events = result.events.map(rec)
              .filter((event): event is Rec => event !== null);
            unassociatedEventCount = events.filter(
              event => typeof event.incident_ref !== 'string' ||
                !event.incident_ref.trim(),
            ).length;
            events = events.filter(event => event.incident_ref === auditId);
          } else if (intent.service) {
            const history = rec(await apiRequest<unknown>(
              '/incidents/history?source=operational',
            ));
            if (!history || !Array.isArray(history.incidents)) {
              throw new Error('The backend returned an unexpected incident history response.');
            }
            const ids = new Set(history.incidents.map(rec)
              .filter((item): item is Rec => item !== null)
              .filter(item => item.service === intent.service)
              .map(item => item.incident_id)
              .filter((id): id is string => typeof id === 'string'));
            const result = await getAuditEvents(undefined, 1000, 'operational');
            events = result.events.map(rec)
              .filter((event): event is Rec => event !== null)
              .filter(event => ids.has(String(event.incident_ref)));
          } else {
            const result = await getAuditEvents(undefined, requestedCount, 'operational');
            events = result.events.map(rec)
              .filter((event): event is Rec => event !== null);
          }
          const sorted = sortNewestFirst(events);
          const limited = sorted.slice(0, requestedCount);
          return {
            incident_id: auditId ?? null,
            requested_count: requestedCount,
            available_count: sorted.length,
            displayed_count: limited.length,
            unassociated_event_count: auditId ? unassociatedEventCount : 0,
            events: limited,
          };
        },
        data => {
          const result = rec(data);
          const count = typeof result?.displayed_count === 'number' ? result.displayed_count : 0;
          if (auditId && count === 0) {
            const unassociatedCount = typeof result?.unassociated_event_count === 'number'
              ? result.unassociated_event_count
              : 0;
            return unassociatedCount
              ? `No audit events demonstrably associated with incident ${auditId} were found. ${unassociatedCount} returned event${unassociatedCount === 1 ? ' lacks' : 's lack'} the incident reference needed to associate it.`
              : `No audit events were found for incident ${auditId}.`;
          }
          return `Showing ${count} of up to ${requestedCount} requested audit entr${count === 1 ? 'y' : 'ies'}, newest first.`;
        },
        'audit',
      );
    } else if (intent.kind === 'timeline') {
      if (intent.incidentId || incidentId) {
        void runIncidentTimeline(raw, (intent.incidentId || incidentId)!);
      } else {
        push({ role: 'user', text: raw });
        push({
          role: 'agent',
          text: 'Include the incident ID whose stored timeline you want to view.',
        });
      }
    } else if (intent.kind === 'recommendation') {
      if (intent.incidentId) {
        void runIncidentLookup(raw, intent.incidentId);
      } else if (selected) {
        push({ role: 'user', text: raw });
        push({
          role: 'answer',
          title: 'Recorded incident recommendations',
          summary: text(incidentPolicy?.action, 'No policy outcome is recorded for this incident.')
            + (simulated ? ' This is simulated demo evidence; no real workload was affected.' : ''),
          data: { diagnosis, policy: incidentPolicy },
        });
      } else if (intent.service) {
        void runReadQuery(
          raw,
          'Current deterministic policy recommendation',
          async () => apiRequest<unknown>(`/decide/${encodeURIComponent(intent.service!)}`),
          data => {
            const result = rec(data);
            const decision = rec(result?.decision);
            return decision
              ? `${text(result?.service)} · ${text(decision.action)} · ${text(decision.reason)} Automatic execution: ${text(decision.automatic_execution, 'not reported')}.`
              : 'No policy decision was returned.';
          },
        );
      } else {
        push({ role: 'user', text: raw });
        push({ role: 'agent', text: `Which service or incident should I inspect? Supported services: ${SERVICES.join(', ')}.` });
      }
    } else if (intent.kind === 'incident') {
      if (incidentId) void runIncidentLookup(raw, incidentId);
      else {
        push({ role: 'user', text: raw });
        push({ role: 'agent', text: 'Please include an incident ID (for example, inc-123), or open an incident first so I can show its stored evidence and audit trail.' });
      }
    } else if (intent.kind === 'ambiguous') {
      push({ role: 'user', text: raw });
      push({
        role: 'agent',
        text: intent.service
          ? `Do you want live health, Prometheus metrics, or the policy recommendation for ${intent.service}?`
          : 'What would you like to check: service health, telemetry, an incident, Kubernetes observations, or audit history?',
      });
    } else {
      push({ role: 'user', text: raw });
      if (/\b(remediat\w*|restart|stop|scale|delete|kill|fix|repair)\b/i.test(raw)) {
        if (intent.service) pendingSafeFollowUp.current = intent.service;
        push({
          role: 'agent',
          text: selected
            ? 'I cannot run commands from chat. Click Start investigation to see the remediation options for this incident. Each executable option has its own Execute button and needs your approval first.'
            : intent.service
            ? `I cannot execute remediation here. I can show the policy recommendation or read-only health for ${intent.service}. Reply with which safe option you want; a generic "yes" will only ask for clarification.`
            : 'I cannot execute remediation here. I can show read-only health or a policy recommendation when you specify a configured service. No action was taken.',
        });
        return;
      }
      push({
        role: 'agent',
        text: 'I could not match that to a supported read-only operation. I can check live service health or metrics, inspect Kubernetes observations in '
          + DEMO_NAMESPACE + ', look up persisted incidents, read audit history, or show policy recommendations. Unsupported request types, including log searches, are not supported here; direct remediation is not supported here.',
      });
    }
  };
  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); submit(); }
  };
  const reset = () => { setMessages([]); setDraft(''); setChatId(newChatId()); hydrated.current = true; if (incidentId || investigationParam) nav('/console'); };

  /* ---------- selected-incident gating ---------- */
  if (incidentId && (loaded.status === 'loading' || loaded.status === 'idle') && !selected) {
    return <Page title="Incident investigation" sub="Loading the persisted incident record.">
      <div className="panel muted" role="status"><span className="spinner" /> Loading backend data…</div>
      <Link className="btn" to="/console">Back to Console</Link>
    </Page>;
  }
  if (incidentId && loaded.status === 'error') {
    return <Page title="Incident investigation" sub="The persisted incident record could not be loaded.">
      <div className="panel" role="alert">
        <p className="err">{loaded.error}</p>
        <div className="row gap wrap" style={{ marginTop: 10 }}>
          <button className="btn" onClick={loaded.reload}>Retry</button>
          <Link className="btn" to="/console">Back to Console</Link>
        </div>
      </div>
    </Page>;
  }
  if (incidentId && loaded.status === 'notfound') {
    return <Page title="Incident not found" sub="This incident ID does not exist in persisted incident records.">
      <div className="panel">
        <p>Incident ID: <code>{incidentId}</code></p>
        <div className="row gap wrap" style={{ marginTop: 10 }}>
          <button className="btn" onClick={loaded.reload}>Check again</button>
          <Link className="btn" to="/console">Back to Console</Link>
        </div>
      </div>
    </Page>;
  }

  /* ---------- derive what the panels show ---------- */
  const evidence = rec(selected?.source_evidence);
  const diagnosis = rec(selected?.diagnosis);
  const incidentPolicy = rec(diagnosis?.policy_recommendation);
  const simulated = isDemo(selected);
  const recordedEvidence = Array.isArray(evidence?.evidence)
    ? evidence.evidence.find((item): item is string => typeof item === 'string')
    : undefined;
  const observedTrigger = selected
    ? incidentRecordText(
      evidence?.description || recordedEvidence || selected.message,
    )
    : '';
  const diagnosisState = observedTrigger
    ? `Recorded trigger: ${observedTrigger}`
    : selected?.diagnosis_status
      ? `Saved diagnosis status: ${text(selected.diagnosis_status)}`
      : 'No additional failure details were saved';
  const recordedRca = rec(diagnosis?.root_cause_analysis) || {};
  const rcaText = typeof diagnosis?.root_cause_analysis === 'string'
    ? diagnosis.root_cause_analysis
    : recordedRca.root_cause || recordedRca.summary;
  const persistedAnalysisNote = diagnosis || selected?.diagnosis_status
    ? 'Incident facts below come from the saved record. Opening it does not rerun analysis or start remediation.'
    : 'Incident details below summarize the saved alert evidence. Opening this record does not run an investigation or start remediation.';

  const wfResult = latestWorkflow?.result;
  const wfPolicy = rec(wfResult?.policy);
  const wfKube = rec(wfResult?.kubernetes);
  const wfDeployments = Array.isArray(wfKube?.deployments) ? wfKube!.deployments as unknown[] : [];
  const wfPersistence = rec(wfResult?.telemetry_incident_persistence);

  const policy = selected ? incidentPolicy : wfPolicy;
  const recs = collectRecs(policy, selected ? diagnosis : rec(wfResult?.diagnosis));
  const approvalRequired = policy?.requires_human_approval;

  const startedAt = selected
    ? text(evidence?.created_at || selected.opened_at, '')
    : text(wfKube?.observed_at, '');
  const alertName = selected ? text(evidence?.scenario || selected.incident_type) : null;
  const isKubernetesTarget = selected?.service === 'kubernetes-lab' || evidence?.source === 'kubernetes';
  const hasApplicationServiceScope = Boolean(
    selected && !isKubernetesTarget && SERVICES.includes(selected.service as Service),
  );
  const scopeLabel = hasApplicationServiceScope ? 'Scope' : 'Namespace';
  const scopeValue = selected
    ? hasApplicationServiceScope
      ? 'Application telemetry'
      : text(evidence?.namespace || rec(evidence?.workload_reference)?.namespace, 'Not recorded')
    : text(wfKube?.namespace, '');
  const resourceLabel = hasApplicationServiceScope ? 'Affected service' : 'Resource';
  const resourceName = selected
    ? text(typeof evidence?.workload_reference === 'string' ? evidence.workload_reference
      : rec(evidence?.workload_reference)?.name || evidence?.resource
        || (hasApplicationServiceScope ? selected.service : undefined), 'Not recorded')
    : null;

  const suggestions: [ReactNode, string, () => void, boolean][] = [
    [`What's happening with the ${service}?`, 'Runs a live advisory workflow', () => void runAdvisory(service), canRun && !running],
    [latestReal ? `Open the latest incident (${latestReal.incident_id})` : 'No operational incidents to open',
      'Opens persisted evidence, no rerun', () => latestReal && openIncident(String(latestReal.incident_id)), Boolean(latestReal)],
    ['Show me recent incidents in the cluster', 'Opens Incidents & Alerts', () => nav('/incidents'), true],
  ];

  /* ---------- right panel ---------- */
  const focusIncidentId = selected ? String(selected.incident_id) : (typeof wfResult?.incident_id === 'string' ? wfResult.incident_id : null);
  const incidentApprovals = (store.approvals.data || []).filter(item => focusIncidentId !== null && item.incident_ref === focusIncidentId) as Rec[];
  const executionOutcome = focusIncidentId ? executionOutcomes[focusIncidentId] : undefined;
  const remediationMode = focusIncidentId ? remediationModes[focusIncidentId] : undefined;
  const incidentIsResolved = Boolean(selected) && (
    String(selected?.status).toUpperCase() === 'RESOLVED'
    || resolvedIds.includes(String(selected?.incident_id)))
    || executionOutcome?.status === 'resolved';
  const resolutionReason = text(selected?.resolution_reason, '');
  const resolutionMessage = ['condition_cleared', 'telemetry_condition_cleared',
    'healthy_service_observation'].includes(resolutionReason)
    ? 'Resolved: fresh observation confirmed the condition cleared; no remediation was executed.'
    : resolutionReason === 'verified_kubernetes_recovery'
      || (!resolutionReason && executionOutcome?.status === 'resolved')
      ? 'Resolved: recovery was verified.'
      : 'Resolved: see the audit history for the recorded resolution reason.';
  const latestOptions = [...messages].reverse().find(item =>
    item.role === 'options' && item.incidentId === focusIncidentId) as
    Extract<Message, { role: 'options' }> | undefined;
  const optionList = (Array.isArray(latestOptions?.data?.options) ? latestOptions!.data.options : [])
    .map(rec).filter((item): item is Rec => item !== null);
  const executableOptions = optionList.filter(item => item.kind === 'executable');
  const runnableOptions = executableOptions.filter(optionExecutionAvailable);
  const policyBlock = executableOptions.find(
    item => item.policy_available === false && typeof item.policy_detail === 'string',
  );
  const runbookReasons = executableOptions
    .map(item => item.runbook_reason)
    .filter((reason): reason is string => typeof reason === 'string' && reason.length > 0);
  const localServiceIncident = rec(selected?.source_evidence)?.simulation === 'local_service_failure';
  const remediationReadiness = !latestOptions
    ? {
      label: 'Not assessed',
      detail: 'Run an investigation to check current evidence and remediation prerequisites.',
      badge: 'pending',
    }
    : runnableOptions.length > 0
      ? !localServiceIncident && (remediationMode?.mode === 'dry_run' || executionOutcome?.status === 'dry_run')
      ? {
        label: 'Candidate available · dry-run only',
        detail: `${runnableOptions.length} action candidate${runnableOptions.length === 1 ? '' : 's'} passed the runbook check, but the backend is configured not to execute commands. Approval will not change the cluster.`,
        badge: 'pending',
      }
      : {
        label: 'Ready to remediate',
        detail: localServiceIncident
          ? `${runnableOptions.length} fix${runnableOptions.length === 1 ? '' : 'es'} passed the runbook and safety checks. Run it from the chat once you approve.`
          : `${runnableOptions.length} action candidate${runnableOptions.length === 1 ? '' : 's'} passed the runbook check. Policy and approval checks still apply.`,
        badge: 'healthy',
      }
      : policyBlock
        ? {
          label: 'Unavailable — action limit reached',
          detail: text(policyBlock.policy_detail),
          badge: 'blocked',
        }
      : latestOptions.data.live_evidence !== true
        ? {
          label: 'Unavailable — current evidence not confirmed',
          detail: text(
            latestOptions.data.note,
            'The current cluster observation does not confirm the exact incident target.',
          ),
          badge: 'blocked',
        }
        : runbookReasons.includes('minimum_ready_replicas_not_met')
          ? {
            label: 'Unavailable — runbook prerequisite not met',
            detail: (() => {
              const blocked = executableOptions.find(
                item => item.runbook_reason === 'minimum_ready_replicas_not_met',
              );
              return blocked
                ? replicaPrerequisiteMessage(blocked)
                  || 'The runbook requires more ready replicas before allowing these actions.'
                : 'The runbook requires more ready replicas before allowing these actions.';
            })(),
            badge: 'blocked',
          }
          : {
            label: 'Guidance only',
            detail: runbookReasons.length
              ? runbookReasons.map(reason => reason.replace(/_/g, ' ')).join('; ')
              : 'No executable action passed the runbook check.',
            badge: 'blocked',
          };
  const rightTabs: [string, string][] = [
    ['investigation', 'Investigation'], ['evidence', 'Evidence'],
    ['remediation', `Remediation (${optionList.length || incidentApprovals.length || recs.length})`], ['audit', 'Audit'],
  ];

  const investigationTab = () => {
    if (selected) return <>
      {incidentIsResolved && <div className="resolvedbar" role="status">✓ {resolutionMessage}</div>}
      <div className="rh"><h3>Alert Details</h3><Badge v={text(selected.severity, 'unknown')}>{text(selected.severity, 'unknown')}</Badge></div>
      <dl className="kvl">
        <Row k="Alert Name">{alertName}</Row>
        <Row k={scopeLabel}>{scopeValue}</Row>
        <Row k={resourceLabel}>{resourceName}</Row>
        <Row k="Service">{text(selected.service)}</Row>
        <Row k="First Seen">{fmt(String(selected.opened_at || ''))}</Row>
        <Row k="Last Seen">{fmt(String(selected.last_seen_at || ''))}</Row>
        <Row k="Status">
          {incidentIsResolved ? <Badge v="resolved">RESOLVED</Badge> : <Badge v={text(selected.status, 'unknown').toLowerCase()}>{text(selected.status, 'unknown')}</Badge>}
        </Row>
        {incidentIsResolved && resolutionReason && <Row k="Resolution">{resolutionMessage.replace(/^Resolved: /, '')}</Row>}
        {!incidentIsResolved && <Row k="Remediation eligibility">
          <Badge v={remediationReadiness.badge}>{remediationReadiness.label}</Badge>
          <p className="tiny muted">{remediationReadiness.detail}</p>
        </Row>}
        {!incidentIsResolved && latestOptions && !localServiceIncident && <Row k="Execution mode">
          {remediationMode?.mode === 'live'
            ? <><Badge v="pending">LIVE · guarded</Badge>
              <p className="tiny muted">Execution still requires a persisted approval, current policy checks, and successful recovery verification.</p></>
            : remediationMode?.mode === 'dry_run' || executionOutcome?.status === 'dry_run'
              ? <><Badge v="blocked">DRY RUN · WILL NOT EXECUTE</Badge>
                <p className="tiny muted">The approval records the planned command only. No cluster change or incident resolution will occur.</p></>
              : <><Badge v="pending">Unknown</Badge>
                <p className="tiny muted">{remediationMode?.error
                  ? `Could not confirm backend execution mode: ${remediationMode.error}`
                  : 'Execution mode was not reported by the backend. Do not assume an approved action will run.'}</p></>}
        </Row>}
        <Row k="Message">{incidentRecordText(selected.message)}</Row>
      </dl>
      <div className="rh"><h3>Investigation State</h3></div>
      <dl className="kvl">
        <Row k="Failure details">{diagnosisState}</Row>
        {rcaText && <Row k="Recorded root-cause analysis">{text(rcaText)}</Row>}
        {diagnosis?.root_cause_analysis_error && <Row k="RCA status">
          {text(diagnosis.root_cause_analysis_status, 'Analysis unavailable')}
          {` · ${text(diagnosis.root_cause_analysis_error)}`}
        </Row>}
        {incidentPolicy?.action && <Row k="Policy">{text(incidentPolicy.action)}</Row>}
        <Row k="Approval">{typeof approvalRequired === 'boolean'
          ? (approvalRequired ? 'Human approval required' : 'Not required by policy') : 'Not reported'}</Row>
        {executionOutcomes[String(selected.incident_id)] && <Row k="Remediation outcome">
          <Badge v={executionOutcomes[String(selected.incident_id)].status}>
            {executionOutcomes[String(selected.incident_id)].status === 'dry_run'
              ? 'DRY RUN · NOT EXECUTED'
              : executionOutcomes[String(selected.incident_id)].status === 'resolved'
                ? 'RESOLVED'
                : 'NOT RESOLVED'}
          </Badge>
          <p className="tiny muted">{executionOutcomes[String(selected.incident_id)].detail}</p>
        </Row>}
      </dl>
    </>;
    if (wfResult) return <>
      <div className="rh"><h3>Advisory result</h3>
        <Badge v={text(wfResult.contract_status || wfResult.rca_status, 'returned')}>{text(wfResult.contract_status || wfResult.rca_status, 'returned')}</Badge></div>
      <dl className="kvl">
        <Row k="Service">{text(wfResult.service)}</Row>
        <Row k="Observed">{fmt(String(wfKube?.observed_at || ''))}</Row>
        <Row k="Namespace">{text(wfKube?.namespace, 'Not reported')}</Row>
        <Row k="Policy">{text(wfPolicy?.action, 'Not reported')}</Row>
        <Row k="Approval">{typeof approvalRequired === 'boolean'
          ? (approvalRequired ? 'Human approval required' : 'Not required by policy') : 'Not reported'}</Row>
        <Row k="RCA">{text(wfResult.rca_status, 'Not reported')}{wfResult.rca_error ? ` · ${text(wfResult.rca_error)}` : ''}</Row>
        <Row k="Persistence">{text(wfPersistence?.status, 'Not reported')}{wfPersistence?.reason ? ` · ${text(wfPersistence.reason)}` : ''}</Row>
      </dl>
      <div className="rh"><h3>Current State</h3>
        <Badge v={wfKube ? 'info' : 'pending'}>{wfKube ? 'Observed' : 'Unavailable'}</Badge></div>
      {wfDeployments.length
        ? wfDeployments.map((entry, index) => {
          const item = rec(entry) || {};
          return <dl className="kvl" key={String(item.name ?? index)}>
            <Row k="Deployment">{text(item.name)}</Row>
            <Row k="Replicas">{text(item.ready_replicas, '?')} ready / {text(item.desired_replicas, '?')} desired</Row>
            <Row k="Available">{text(item.available_replicas)}</Row>
          </dl>;
        })
        : <p className="muted">{wfKube ? 'The observation listed no deployments.' : 'Kubernetes observations were not returned by this workflow run.'}</p>}
    </>;
    return <>
      <div className="rh"><h3>No incident selected</h3></div>
      <p className="muted">Pick an incident to load its persisted evidence, diagnosis, RCA, policy and audit trail. Nothing is rerun when you open one.</p>
      <div className="rh"><h3>Open incidents</h3>
        {openIncidentList.length > 0 && <span className="tiny muted">{openIncidentList.length} open</span>}</div>
      {store.incidents.status !== 'ready'
        ? <ResourceMessage resource={store.incidents} retry={() => void refresh('incidents')} />
        : openIncidentList.map(item =>
          <button className="qa open-inc" key={`${String(item.id ?? '')}-${String(item.opened_at ?? '')}-${String(item.incident_id)}`} onClick={() => openIncident(String(item.incident_id))}>
            <Badge v={text(item.severity, 'unknown')}>{text(item.severity, 'unknown')}</Badge>
            <span className="oi-id">{text(item.incident_id)}</span>
            <span className="oi-type">{text(item.incident_type, 'unknown type').replace(/_/g, ' ')}</span>
          </button>)}
      {store.incidents.status === 'ready' && openIncidentList.length === 0 &&
        <Empty text="No open incidents." />}
    </>;
  };

  const evidenceTab = () => {
    if (selected) return evidence
      ? <><p className="tiny muted">Recorded source evidence · values are shown as stored.</p><Json value={evidence} /></>
      : <Empty text="No source evidence is stored for this record." />;
    if (wfResult) return <>
      <p className="tiny muted">Returned by the advisory workflow. Hypotheses are not verified findings.</p>
      {(['telemetry_anomaly', 'kubernetes', 'diagnosis'] as const).map(key =>
        <details key={key} open={key === 'telemetry_anomaly'}>
          <summary>{key.replace('_', ' ')}</summary>
          {wfResult[key] == null ? <p className="muted">Not included in this response.</p> : <Json value={wfResult[key]} />}
        </details>)}
    </>;
    return <Empty text="Evidence appears here once an incident is open or an advisory workflow has run." />;
  };

  const remediationTab = () => <RemediationPanel approvals={incidentApprovals} events={timeline.events} policy={policy}
    recs={recs} options={optionList} incidentType={text(selected?.incident_type, text(latestOptions?.data.incident_type, ''))}
    readinessNote={text(latestOptions?.data.note, '')}
    resolved={incidentIsResolved} resolvedMessage={resolutionMessage} outcome={executionOutcome}
    assessmentRequired={Boolean(selected && !latestOptions && !incidentIsResolved)}
    canAssess={Boolean(selected && canRun && !running && ['OPEN', 'REMEDIATING'].includes(String(selected.status)))}
    onAssess={() => void startInvestigation()}
    canRun={store.permissions.includes('remediation')} busy={running} onExecute={executeOption}
    executeInChat={Boolean(latestOptions)} />;

  const auditTab = () => {
    if (!selected) return <Empty text="Open an incident to see its audit timeline." />;
    return <>
      <div className="row between">
        <span className="tiny muted">Recorded events for {text(selected.incident_id)}</span>
        {timeline.status === 'ready' && <button className="btn sm" onClick={() => setTimelineReload(n => n + 1)}>Refresh timeline</button>}
      </div>
      {timeline.status === 'loading' && <p className="muted" role="status">Loading audit timeline…</p>}
      {timeline.status === 'error' && <>
        <p className="err" role="alert">{timeline.error}</p>
        <button className="btn sm" onClick={() => setTimelineReload(n => n + 1)}>Retry timeline</button>
      </>}
      {timeline.status === 'ready' && (timeline.events.length
        ? <div className="aflow-side">
          <AuditFlowCard events={timeline.events} incidentId={text(selected.incident_id)} incident={selected} title="Audit flow" />
        </div>
        : <Empty text="The backend returned no audit events for this incident." />)}
    </>;
  };

  const panes: Record<string, () => ReactNode> = {
    investigation: investigationTab, evidence: evidenceTab, remediation: remediationTab, audit: auditTab,
  };

  const showHero = !selected && messages.length === 0;
  const authLabel = store.permissions.length ? store.permissions.join(', ') : 'Not signed in';

  return <div className="cwrap">
    {/* context strip */}
    <div className="ctx">
      {selected ? <>
        <div><small>Investigation ID</small><span className="row gap">{text(selected.incident_id)} <CopyBtn text={String(selected.incident_id)} /></span></div>
        <div><small>Alert</small>{alertName}</div>
        <div><small>{scopeLabel}</small>{scopeValue}</div>
        <div><small>{resourceLabel}</small><span className="row gap">{resourceName}{resourceName !== 'Not recorded' && <CopyBtn text={String(resourceName)} />}</span></div>
        <div><small>Severity</small><Badge v={text(selected.severity, 'unknown')}>{text(selected.severity, 'unknown')}</Badge></div>
        <div><small>Status</small>{incidentIsResolved ? <Badge v="resolved">RESOLVED</Badge> : <Badge v={text(selected.status, 'unknown').toLowerCase()}>{text(selected.status, 'unknown')}</Badge>}</div>
        <div><small>Started</small>{fmt(startedAt)}</div>
      </> : <>
        <div><small>Service</small>
          <select aria-label="Service" value={service} onChange={event => setService(event.target.value as Service)}>
            {SERVICES.map(item => <option key={item} value={item}>{item}</option>)}
          </select></div>
        <div><small>Workflow</small>Advisory diagnosis</div>
        <div><small>Authorization</small>{authLabel}</div>
        {wfResult && <div><small>Last result</small><span className="row gap">{text(wfResult.service)} <CopyBtn text={String(wfResult.service)} /></span></div>}
        {wfPolicy && <div><small>Policy outcome</small><Badge v={text(wfPolicy.action, 'unknown')}>{text(wfPolicy.action, 'unknown')}</Badge></div>}
        {typeof approvalRequired === 'boolean' && <div><small>Approval</small>{approvalRequired ? 'Required' : 'Not required by policy'}</div>}
      </>}
    </div>

    <div className="work">
      {/* conversation / investigation column */}
      <section className="chat" aria-label="Investigation">
        <div className={`msgs ${showHero ? 'empty-state' : ''}`}>
          {selected && <h1 className="sr-only">Incident investigation</h1>}
          {showHero && <div className="kb-hero">
            <span className="kb-mark"><LogoMark size={38} /></span>
            <h1>Kybernaut Agent</h1>
            <p>Investigate incidents, diagnose root causes, and review approved remediation workflows.</p>
            <ul className="kb-sugg" aria-label="Suggested actions">
              {suggestions.map(([label, hint, action, enabled], index) =>
                <li key={index}><button disabled={!enabled} onClick={action}>
                  <span className="kb-ic">{index === 0 ? <AlertTriangle size={16} />
                    : index === 1 ? <Sparkles size={16} /> : <History size={16} />}</span>
                  <span className="kb-tx"><span>{label}</span><small>{hint}</small></span>
                  <ChevronRight size={16} className="kb-go" />
                </button></li>)}
            </ul>
          </div>}

          {selected && <>
            <div className="ai">
              <p><b>Persisted investigation loaded.</b></p>
              <p className="muted">{persistedAnalysisNote}</p>
              <dl className="kvl">
                <Row k="Scenario / type">{alertName}</Row>
                <Row k="Status">{text(selected.status)}</Row>
                {resolutionReason && <Row k="Resolution">{resolutionMessage.replace(/^Resolved: /, '')}</Row>}
                <Row k="Failure details">{diagnosisState}</Row>
                {rcaText && <Row k="Recorded root-cause analysis">{text(rcaText)}</Row>}
                {incidentPolicy?.action && <Row k="Policy outcome">{text(incidentPolicy.action)}</Row>}
              </dl>
              {(() => {
                const facts = (Array.isArray(evidence?.evidence) ? evidence.evidence : [])
                  .filter((item): item is string => typeof item === 'string').map(incidentRecordText).slice(0, 4);
                const sameService = (store.incidents.data || []).filter(item =>
                  item.service === selected.service && item.incident_id !== selected.incident_id);
                const sameOpen = sameService.filter(item => ['OPEN', 'REMEDIATING'].includes(String(item.status).toUpperCase()));
                const openedMs = Date.parse(String(selected.opened_at || ''));
                const endMs = selected.resolved_at ? Date.parse(String(selected.resolved_at)) : Date.now();
                const duration = Number.isFinite(openedMs) && Number.isFinite(endMs) && endMs >= openedMs
                  ? formatDuration((endMs - openedMs) / 1000) : '—';
                const lastEvent = timeline.events.length ? timeline.events[timeline.events.length - 1] : null;
                const metric = rec(evidence?.metric);
                return <div className="pl-collect" aria-label="Collected incident information">
                  <div className="pl-chips">
                    <span><small>Severity</small><b>{text(selected.severity)}</b></span>
                    <span><small>Opened</small><b>{fmt(String(selected.opened_at || ''))}</b></span>
                    <span><small>{selected.resolved_at ? 'Time to resolve' : 'Open for'}</small><b>{duration}</b></span>
                    <span><small>Approvals</small><b>{incidentApprovals.length}</b></span>
                    <span><small>Audit events</small><b>{timeline.status === 'ready' ? timeline.events.length : '…'}</b></span>
                    <span><small>Same-service incidents</small><b>{sameService.length}{sameOpen.length ? ` (${sameOpen.length} open)` : ''}</b></span>
                  </div>
                  {facts.length > 0 && <div className="pl-collect-block"><h5>Recorded findings</h5>
                    <ul>{facts.map((fact, index) => <li key={index}>{fact}</li>)}</ul></div>}
                  {metric && <div className="pl-collect-block"><h5>Recorded metric</h5>
                    <p className="tiny">{text(metric.name)}: value {text(metric.value)} against baseline {text(metric.baseline_median)} (deviation {text(metric.deviation_score)}).</p></div>}
                  <div className="pl-collect-block"><h5>Handling so far</h5>
                    <p className="tiny">{incidentApprovals.length
                      ? `${incidentApprovals.length} approval record${incidentApprovals.length === 1 ? '' : 's'} on file; latest is ${text(incidentApprovals[0].status)}.`
                      : 'No approval has been requested.'}
                    {' '}{selected.resolved_at ? `Resolved ${fmt(String(selected.resolved_at))}.` : 'Remediation has not been executed from this view.'}
                    {lastEvent?.timestamp ? ` Last audit event recorded ${fmt(String(lastEvent.timestamp))}.` : ''}</p></div>
                </div>;
              })()}
              <div className="row gap wrap" style={{ marginTop: 10 }}>
                {['OPEN', 'REMEDIATING'].includes(String(selected.status)) && <button className="btn sm primary" disabled={running || !canRun} onClick={() => void startInvestigation()}>Start investigation</button>}
                <Link className="btn sm" to={`/incidents/${encodeURIComponent(incidentId!)}`}>Open incident record</Link>
                <button className="btn sm" disabled={running || !canRun}
                  onClick={() => void runServiceHealth(`How is ${String(selected.service)} doing?`, String(selected.service))}>Service health</button>
              </div>
            </div>
            {diagnosis && <details className="card"><summary><b>Recorded diagnosis and RCA</b></summary><Json value={diagnosis} /></details>}
          </>}

          {messages.map(message => {
            if (message.role === 'user') return <div className="me" key={message.id}>{message.text}</div>;
            if (message.role === 'agent') return <div className={`ai ${message.tone === 'error' ? 'errbox' : ''}`} key={message.id} role={message.tone === 'error' ? 'alert' : undefined}>{message.text}</div>;
            if (message.role === 'steps') return <StepsBubble key={message.id} title={message.title} steps={message.steps} />;
            if (message.role === 'assist') return <AssistBubble key={message.id} result={message.result} />;
            if (message.role === 'report') return <ReportCard key={message.id} report={message.report} />;
            if (message.role === 'options') {
              return <OptionBubbles key={message.id} data={message.data} inline
                canRun={store.permissions.includes('remediation')} resolved={incidentIsResolved}
                busy={running} onExecute={executeOption}
                analysing={Boolean(message.analysing) && running}
                onDismiss={() => push({ role: 'agent', text: 'Okay, nothing was run. Ask me anything about this incident, or run the investigation again later.' })} />;
            }
            if (message.role === 'remediation') {
              return <InvestigationBubbles key={message.id} result={message.result} approvals={(store.approvals.data || []) as Rec[]}
                canDecide={store.permissions.includes('remediation')} onExecute={executeApproval} />;
            }
            if (message.role === 'answer') {
              const answer = rec(message.data);
              const answerEvents = Array.isArray(answer?.events)
                ? answer.events.map(rec).filter((item): item is Rec => item !== null)
                : [];
              const answerServices = Array.isArray(answer?.services)
                ? answer.services.map(rec).filter((item): item is Rec => item !== null)
                : [];
              const configuredServices = Array.isArray(answer?.services)
                ? answer.services.filter((item): item is string => typeof item === 'string')
                : [];
              const telemetryServices = rec(answer?.services) || {};
              if (message.layout === 'service_health' && answer) {
                return <ServiceHealthCard key={message.id} data={answer as unknown as ServiceHealthData} />;
              }
              const auditScope = typeof answer?.incident_id === 'string' ? answer.incident_id : null;
              if (answerEvents.length > 0 && ((message.layout === 'audit' && auditScope) || message.layout === 'timeline')) {
                return <div className="flowgroup" key={message.id}>
                  <AuditFlowCard events={answerEvents} incidentId={auditScope}
                    incident={selected && String(selected.incident_id) === auditScope ? selected : null}
                    title={message.layout === 'timeline' ? 'Incident timeline' : 'Audit flow'} />
                  <details className="tiny">
                    <summary>Evidence returned by the backend</summary>
                    <Json value={message.data} />
                  </details>
                </div>;
              }
              return <div className="ai" key={message.id}>
                <b>{message.title}</b>
                <AnswerSummary textValue={message.summary} />
                {message.layout === 'audit' && <div className="scroll">
                  <table>
                    <caption>Audit records, newest first</caption>
                    <thead><tr>
                      <th>Timestamp</th><th>Event type</th><th>Actor</th><th>Resource</th><th>Outcome</th>
                    </tr></thead>
                    <tbody>{answerEvents.map((event, index) => <tr key={String(event.event_id || `${event.timestamp}-${event.event_type}-${index}`)}>
                      <td>{text(event.timestamp)}</td>
                      <td>{text(event.event_type)}</td>
                      <td>{text(event.actor, 'Unknown')}</td>
                      <td>{text(event.resource, 'Not recorded')}</td>
                      <td>{text(event.outcome, 'Not recorded')}</td>
                    </tr>)}</tbody>
                  </table>
                </div>}
                {message.layout === 'health' && <div className="scroll">
                  <table>
                    <caption>Live health evidence</caption>
                    <thead><tr><th>Service</th><th>Status</th><th>Freshness</th><th>Finding / error</th></tr></thead>
                    <tbody>{answerServices.map((item, index) => {
                      const findings = Array.isArray(item.findings)
                        ? item.findings.map(rec).filter((finding): finding is Rec => finding !== null)
                          .filter(finding => finding.type !== 'no_detected_anomaly')
                          .map(finding => `${text(finding.type)}: ${text(finding.evidence)}`)
                        : [];
                      const status = item.health_status === 'healthy' && item.freshness_status === 'fresh'
                        ? 'HEALTHY'
                        : item.health_status === 'unhealthy'
                          ? 'UNHEALTHY'
                          : item.health_status === 'degraded'
                            ? 'DEGRADED'
                            : item.freshness_status === 'stale'
                              ? 'STALE'
                              : 'UNKNOWN';
                      return <tr key={String(item.service || index)}>
                        <td>{text(item.service)}</td><td>{status}</td>
                        <td>{text(item.freshness_status, 'Not reported')}</td>
                        <td>{text(item.error, findings.join('; ') || 'No anomaly finding recorded.')}</td>
                      </tr>;
                    })}</tbody>
                  </table>
                </div>}
                {message.layout === 'telemetry' && <div className="scroll">
                  <table>
                    <caption>Live Prometheus telemetry</caption>
                    <thead><tr><th>Service</th><th>CPU cores</th><th>Memory bytes</th><th>Request rate</th><th>P95 latency</th><th>Observed</th></tr></thead>
                    <tbody>{Object.entries(telemetryServices).map(([name, value]) => {
                      const features = rec(value);
                      return <tr key={name}>
                        <td>{name}</td>
                        <td>{features ? text(features.cpu_cores_used) : 'Unavailable'}</td>
                        <td>{features ? text(features.memory_bytes) : 'Unavailable'}</td>
                        <td>{features ? text(features.request_rate) : 'Unavailable'}</td>
                        <td>{features ? text(features.p95_latency_seconds) : 'Unavailable'}</td>
                        <td>{features ? text(features.observed_at) : 'No complete fresh row'}</td>
                      </tr>;
                    })}</tbody>
                  </table>
                </div>}
                {message.layout === 'services' && <ul>
                  {configuredServices.map(item => <li key={item}>{item}</li>)}
                </ul>}
                {message.data !== undefined && <details>
                  <summary>Evidence returned by the backend</summary>
                  <Json value={message.data} />
                </details>}
              </div>;
            }
            const result = message.result;
            const resultPolicy = rec(result.policy);
            const observation = rec(result.kubernetes);
            const observationIncidents = Array.isArray(observation?.incidents)
              ? observation.incidents.map(rec).filter((item): item is Rec => item !== null)
              : [];
            const persistence = rec(observation?.persistence);
            const persistedIncidents = Array.isArray(result.persisted_incidents)
              ? result.persisted_incidents.map(rec)
                .filter((item): item is Rec => item !== null)
              : [];
            const classification = rec(result.classification);
            const policyDecision = rec(result.policy_decision);
            const proposal = rec(result.proposal);
            const execution = rec(result.execution);
            const verification = rec(result.verification);
            const responseGraph = result.investigation_type === 'response_graph';
            return <div className="ai" key={message.id}>
              <div className="row between"><b>{responseGraph
                ? 'Incident response workflow'
                : result.investigation_type === 'kubernetes_observation'
                  ? 'Kubernetes observation'
                  : 'Advisory result'} · {message.service}</b>
                <Badge v={text(result.status || result.contract_status || result.rca_status || result.kubernetes_status, 'returned')}>{text(result.status || result.contract_status || result.rca_status || result.kubernetes_status, 'returned')}</Badge></div>
              {observation
                ? <>
                  <p className="muted">{observationIncidents.length} current Kubernetes incident(s) observed in {text(observation.namespace)}. Persistence reported {text(persistence?.persisted_count, 'no count')}; persisted history lookup: {text(result.persistence_discovery_status)}. No remediation was executed during observation.</p>
                  {observationIncidents.length
                    ? <ul className="tl">{observationIncidents.map((item, index) => {
                      const evidence = Array.isArray(item.evidence)
                        ? item.evidence.filter((value): value is string => typeof value === 'string')
                        : [];
                      return <li key={`${text(item.type)}-${text(item.resource)}-${index}`}>
                        <b>{text(item.type)} · {text(item.severity)}</b>
                        <span className="tiny muted">{text(item.resource)} · {fmt(text(item.source_timestamp, ''))}</span>
                        {evidence.length > 0 && <p className="tiny">{evidence.join(' · ')}</p>}
                        {item.recommendation && <p className="tiny muted">Observer recommendation (not RCA): {text(item.recommendation)}</p>}
                      </li>;
                    })}</ul>
                    : <p className="tiny muted">No current Kubernetes incidents were observed. This observation-only path does not evaluate RCA or policy.</p>}
                  {observationIncidents.length > 0 && <p className="tiny muted">This observation-only path does not evaluate RCA or policy.</p>}
                  {result.persistence_discovery_error && <p className="err" role="alert">{text(result.persistence_discovery_error)}</p>}
                  {persistedIncidents.length > 0 && <div>
                    <h4>Persisted operational incidents</h4>
                    <ul className="tl">{persistedIncidents.map((item, index) => {
                      const id = String(item.incident_id);
                      const evidence = rec(item.source_evidence);
                      return <li key={`${id}-${index}`}>
                        <b>{text(item.incident_type)} · {text(item.severity)}</b>
                        <span className="tiny muted">{id} · {text(item.status)} · {text(evidence?.resource)}</span>
                        <div className="row gap wrap" style={{ marginTop: 6 }}>
                          <Link className="btn sm" to={`/incidents/${encodeURIComponent(id)}`}>Open incident</Link>
                          {item.status === 'OPEN' || item.status === 'REMEDIATING'
                            ? <button className="btn sm" disabled={running || !canRun} onClick={() => void investigateKubernetesIncident(id)}>
                              Run RCA + policy
                            </button>
                            : <span className="tiny muted">Investigation is limited to active occurrences.</span>}
                        </div>
                      </li>;
                    })}</ul>
                  </div>}
                </>
                : responseGraph
                  ? <details><summary className="tiny">Technical details</summary>
                    <p className="muted">Current observed facts are checked against the selected persisted occurrence. RCA is advisory and does not establish a verified cause.</p>
                    <dl className="kvl">
                      <Row k="Incident">{text(result.incident_id)}</Row>
                      <Row k="Classification">{text(classification?.incident_type, 'unknown')} · confidence {text(classification?.confidence, 'not reported')}</Row>
                      <Row k="Observed facts">{Array.isArray(classification?.evidence)
                        ? (classification.evidence as unknown[]).map(String).join(' · ') || 'No facts recorded'
                        : 'No facts recorded'}</Row>
                      <Row k="RCA status">{text(result.rca_status, 'not reported')}</Row>
                      <Row k="RCA result">{result.rca
                        ? <Json value={result.rca} />
                        : 'No RCA result was returned; see status/error evidence.'}</Row>
                      <Row k="Policy">{policyDecision
                        ? <Json value={policyDecision} />
                        : 'No policy decision was recorded.'}</Row>
                      <Row k="Proposed action">{proposal ? <Json value={proposal} /> : 'No action proposed.'}</Row>
                      <Row k="Approval">{result.approval_id
                        ? <>{text(result.status)} · approval {text(result.approval_id)}</>
                        : text(result.status)}</Row>
                      <Row k="Execution">{execution ? <Json value={execution} /> : 'Not executed.'}</Row>
                      <Row k="Recovery verification">{verification ? <Json value={verification} /> : 'Not run.'}</Row>
                    </dl>
                    <p className="tiny muted">Execution requires a separate authorized human decision. Recovery is reported only after fresh verification.</p>
                  </details>
                  : <p className="muted">Returned by the backend. Hypotheses are not verified findings; no remediation was executed.</p>}
              {!responseGraph && <p className="tiny">Policy: {text(resultPolicy?.action, 'not reported')}
                {typeof resultPolicy?.requires_human_approval === 'boolean' && (resultPolicy.requires_human_approval ? ' · human approval required' : ' · no approval required by policy')}</p>}
              {result.contract_error ? <p className="tiny">Contract validation: {text(result.contract_error)}</p> : null}
              <p className="tiny muted">Details are in the Investigation, Evidence and Remediation tabs.</p>
            </div>;
          })}
          {running && <div className="kb-run" role="status"><span className="typing"><span /><span /><span /></span> Working…</div>}
          <div ref={endRef} />
        </div>

        <div className="composer">
          <textarea
            aria-label="Ask Kybernaut"
            rows={1}
            placeholder="Name a service or paste an incident ID…"
            value={draft}
            onChange={event => setDraft(event.target.value)}
            onKeyDown={onKey}
          />
          <button className="send" aria-label="Send" disabled={!draft.trim() || running} onClick={submit}><Send size={18} /></button>
          <button className="btn" onClick={reset}>New</button>
        </div>
        <p className="composer-note tiny muted">
          Advisory reads current service and incident records. Remediation actions require your approval.
        </p>
      </section>

      {/* right column */}
      <aside className="right">
        <div className="rcard">
          <Tabs tabs={rightTabs} v={tab} set={setTab} />
          <div className="rbody" role="tabpanel" aria-label={tab}>{panes[tab]()}</div>
        </div>
        <div className="rcard"><div className="rbody">
          <h3>Quick Actions</h3>
          {selected && <Link className="qa" to={`/incidents/${encodeURIComponent(incidentId!)}`}>Open incident record<ExternalLink size={15} /></Link>}
          <button className="qa" disabled={!canRun || running}
            onClick={() => { const target = selected ? String(selected.service) : service; void runServiceHealth(`How is ${target} doing?`, target); }}>
            <span><Server size={16} color="#0E8C8F" />Service health</span>
          </button>
          <Link className="qa" to={selected ? `/audit?q=${encodeURIComponent(String(selected.incident_id))}` : '/audit'}>
            <span><ScrollText size={16} color="#0E8C8F" />Audit trail</span><ExternalLink size={15} />
          </Link>
          <Link className="qa" to="/incidents"><span><Bell size={16} color="#0E8C8F" />Incidents &amp; Alerts</span><ExternalLink size={15} /></Link>
          <button className="qa" disabled={!canRun || running} onClick={() => void runAdvisory(service)}>
            <span><Play size={16} color="#0E8C8F" />Run advisory for {service}</span>
          </button>
        </div></div>
      </aside>
    </div>
  </div>;
}