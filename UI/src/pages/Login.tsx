import { useState } from 'react';
import type { FormEvent } from 'react';
import { useLocation, useNavigate, Navigate } from 'react-router-dom';
import { ShieldCheck, Activity, PiggyBank } from 'lucide-react';
import { Logo } from '../ui';
import { signIn, useAuth } from '../store';
import '../polish.css';

// Real workflow, in order: evidence -> diagnosis -> policy -> human review -> guarded action -> verified.
const STEPS: [string, string][] = [
  ['Evidence', 'metrics + events'], ['Diagnosis', 'root cause'], ['Policy', 'rules check'],
  ['Human review', 'approve / reject'], ['Remediate', 'guarded action'], ['Verified', 'recovery check'],
];
const FLOW_X = (index: number) => 52 + index * 116;
const FLOW_Y = 96;
const FLOW_PATH = `M${FLOW_X(0)} ${FLOW_Y} L${FLOW_X(STEPS.length - 1)} ${FLOW_Y}`;
function Topology() {
  const last = STEPS.length - 1;
  return <svg className="topo kbf" viewBox="0 0 680 200" role="img"
    aria-label={`Kybernaut workflow: ${STEPS.map(([name]) => name).join(', then ')}`}>
    {STEPS.slice(0, -1).map(([name], index) => <g key={name}>
      <line className="edge" x1={FLOW_X(index)} y1={FLOW_Y} x2={FLOW_X(index + 1)} y2={FLOW_Y} />
      <line className={`kbf-seg kbf-seg${index}`} x1={FLOW_X(index)} y1={FLOW_Y} x2={FLOW_X(index + 1)} y2={FLOW_Y} />
    </g>)}
    <circle className="kbf-glow" r={13} style={{ offsetPath: `path("${FLOW_PATH}")` }} />
    <circle className="kbf-pulse" r={4.5} style={{ offsetPath: `path("${FLOW_PATH}")` }} />
    {STEPS.map(([name, caption], index) => <g key={name} className={index === last ? 'kbf-final' : ''}>
      {index === last && <circle className="kbf-ring" cx={FLOW_X(index)} cy={FLOW_Y} r={11} />}
      <circle className={`node kbf-node kbf-node${index}`} cx={FLOW_X(index)} cy={FLOW_Y} r={index === last ? 11 : 9} />
      <text className="kbf-step" x={FLOW_X(index)} y={FLOW_Y - 24} textAnchor="middle">{String(index + 1).padStart(2, '0')}</text>
      <text className="kbf-name" x={FLOW_X(index)} y={FLOW_Y + 32} textAnchor="middle">{name}</text>
      <text className="kbf-cap" x={FLOW_X(index)} y={FLOW_Y + 48} textAnchor="middle">{caption}</text>
    </g>)}
  </svg>;
}

export default function Login() {
  const nav = useNavigate();
  const location = useLocation();
  const authenticated = useAuth();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  if (authenticated) return <Navigate to="/console" replace />;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError('');
    setBusy(true);
    try {
      await signIn(username, password);
      setUsername('');
      setPassword('');
      const state = location.state as { from?: string } | null;
      nav(state?.from || '/console', { replace: true });
    } catch (reason) {
      setError(reason instanceof Error
        ? reason.message
        : 'Unable to sign in. Check your credentials and try again.');
    } finally {
      setBusy(false);
    }
  };

  return <div className="login">
    <div className="lbrand"><Logo />
      <div><h1>Reliability and cost, investigated by agents. Approved by humans.</h1><Topology /></div>
      <div className="lfeat">{[[Activity, 'Investigate faster', 'Review service diagnosis and incident evidence from the backend.'], [ShieldCheck, 'Human-approved changes', 'Backend authorization and approval safeguards remain authoritative.'], [PiggyBank, 'Cost reporting', 'View generated reports only when the backend has one available.']].map(([Icon, title, detail]: any) => <div className="feat" key={title}><span className="ic"><Icon size={20} /></span><b>{title}</b><span className="muted">{detail}</span></div>)}</div></div>
    <div className="lform"><form className="lcard" onSubmit={submit}>
      <div className="mlogo"><Logo /></div><h1>Sign in</h1><p className="muted">Use your provisioned Kybernaut operator account.</p>
      {error && <div className="toast err" style={{ marginTop: 16 }} role="alert">{error}</div>}
      <div className="field"><label htmlFor="operator-username">Username</label><input id="operator-username" autoFocus autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} aria-invalid={!!error} /></div>
      <div className="field"><label htmlFor="operator-password">Password</label><input id="operator-password" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} aria-invalid={!!error} /></div>
      <button className="btn primary" disabled={busy || !username.trim() || !password}>{busy ? <><span className="spinner" /> Signing in…</> : 'Sign in'}</button>
      <p className="muted tiny" style={{ marginTop: 16 }}>Accounts and permissions are provisioned by an administrator. Passwords are never stored in this browser; the short-lived access token stays in this tab until it expires or you sign out.</p></form></div></div>;
}