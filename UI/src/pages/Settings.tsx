import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { Page, Tabs, Badge } from '../ui';
import { refresh, useStore } from '../store';
import { config } from '../config';
import { getAgentStatus } from '../api';
import { BackButton } from '../components';
import { SimulatorPanel } from '../simulator';

type Info = Record<string, any>;
const tone = (status: unknown) => (status === 'healthy' ? 'healthy' : status === 'unhealthy' || status === 'unavailable' ? 'critical' : 'pending');

export default function SettingsPage() {
  const state = useStore();
  const [tab, setTab] = useState('integrations');
  const [agent, setAgent] = useState<{ data: Info | null; error: string; busy: boolean }>({ data: null, error: '', busy: false });

  const loadAgent = (probe: boolean) => {
    setAgent(previous => ({ ...previous, busy: true, error: '' }));
    getAgentStatus(probe).then(data => setAgent({ data, error: '', busy: false }))
      .catch(reason => setAgent({ data: null, error: reason instanceof Error ? reason.message : 'Agent status unavailable.', busy: false }));
  };
  useEffect(() => {
    loadAgent(false);
  }, []);

  const rows = (items: [string, ReactNode][]) => <dl className="kv" style={{ gridTemplateColumns: '220px 1fr' }}>
    {items.map(([label, value]) => [<dt key={label}>{label}</dt>, <dd key={`${label}-v`}>{value}</dd>])}
  </dl>;
  const components = (state.health.data?.components || {}) as Record<string, Info>;
  const llm = (agent.data?.llm || {}) as Info;
  const probe = llm.probe as Info | undefined;

  return <Page title="Settings & Integrations" sub="Read-only view of what the backend reports. Configuration is changed in the server environment, not here." actions={<BackButton />}>
    <Tabs tabs={[['integrations', 'Integrations'], ['agent', 'Agent & LLM'], ['lab', 'Incident generator'], ['security', 'Security']]} v={tab} set={setTab} />
    <div className="panel">
      {tab === 'integrations' && rows([
        ['API', <><code>{config.apiBase}</code> · <Badge v={tone(components.api?.status)}>{String(components.api?.status || (state.health.status === 'error' ? 'unavailable' : 'checking'))}</Badge> <button className="btn sm" onClick={() => void refresh('health')}>Check now</button></>],
        ['Prometheus', <><Badge v={tone(components.prometheus?.status)}>{String(components.prometheus?.status || 'unknown')}</Badge> <span className="muted">{String(components.prometheus?.reason || '')}</span></>],
        ['Kubernetes (lab)', <><Badge v={tone(components.kubernetes_lab?.status)}>{String(components.kubernetes_lab?.status || 'unknown')}</Badge> <span className="muted">{String(components.kubernetes_lab?.reason || '')}</span></>],
        ['Database', <><Badge v={tone(components.database?.status)}>{String(components.database?.status || 'unknown')}</Badge> <span className="muted">{String(components.database?.reason || '')}</span></>],
        ['Anomaly monitor', state.monitor.status === 'ready' ? <pre className="json">{JSON.stringify(state.monitor.data, null, 2)}</pre> : <>{state.monitor.status === 'error' ? state.monitor.error : 'Not checked'} <button className="btn sm" onClick={() => void refresh('monitor')}>Check</button></>],
      ])}
      {tab === 'agent' && <>
        {agent.error && <p className="err" role="alert">{agent.error}</p>}
        {rows([
          ['LLM provider', String(llm.provider || '—')],
          ['Model', <code>{String(llm.model || '—')}</code>],
          ['API key', llm.api_key_configured ? <Badge v="healthy">configured</Badge> : <Badge v="critical">not configured</Badge>],
          ['Model name valid', llm.model_name_valid === false ? <Badge v="critical">invalid</Badge> : <Badge v="healthy">valid</Badge>],
          ['Live connectivity', probe ? <><Badge v={probe.status === 'reachable' ? 'healthy' : 'critical'}>{String(probe.status)}</Badge> {probe.error_code ? <span className="muted">{String(probe.error_code)}</span> : null}</> : <span className="muted">Not tested</span>],
          ['Remediation mode', <><code>{String(agent.data?.remediation_mode || '—')}</code> <span className="muted">(dry_run records the command without executing it)</span></>],
          ['Automatic remediation', String(agent.data?.auto_remediation || 'false')],
          ['Same-operator approval', agent.data?.self_approval_allowed
            ? 'Enabled for remediation-authorized operators'
            : 'Unavailable'],
          ['Supported actions', (agent.data?.supported_actions || []).join(', ') || '—'],
          ['Supported incident types', (agent.data?.supported_incident_types || []).join(', ') || '—'],
        ])}
        <div className="row gap" style={{ marginTop: 12 }}>
          <button className="btn" disabled={agent.busy} onClick={() => loadAgent(false)}>Reload</button>
          <button className="btn primary" disabled={agent.busy} onClick={() => loadAgent(true)}>{agent.busy ? 'Testing…' : 'Test LLM connection'}</button>
        </div>
        <p className="tiny muted">The test sends one tiny request to the model provider. The API key is never returned to the browser.</p>
      </>}
      {tab === 'lab' && <SimulatorPanel />}
      {tab === 'security' && rows([
        ['Authentication', 'Operator JWT verified by the backend.'],
        ['Your permissions', state.permissions.length ? state.permissions.join(', ') : 'None'],
        ['Browser token storage', 'Session storage for this tab only; cleared on sign-out.'],
        ['Authorization', 'Enforced by backend permissions on every protected request.'],
        ['Remediation authorization', 'Authenticated permissions, persisted approval, requester separation by default, and fresh backend policy checks.'],
      ])}
    </div>
  </Page>;
}