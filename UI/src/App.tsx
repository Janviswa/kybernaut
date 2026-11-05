import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { NavLink, Route, Routes, Navigate, useNavigate, useLocation, Link } from 'react-router-dom';
import { MessageSquare, LayoutGrid, Server, Bell, History, ScrollText, TrendingDown, Settings, ChevronsLeft, ChevronsRight, UserRound, LogOut, Search, Plus, Menu, ChevronDown } from 'lucide-react';
import { useStore, set, toast, useAuth, signOut, refresh } from './store';
import { SERVICES } from './data';
import { openIncidentsOf } from './openIncidents';
import { Logo, Toaster, Page } from './ui';
import logo from './assets/logo.png';
import Console from './pages/Console'; import Overview from './pages/Overview'; import { Services, ServiceDetail } from './pages/Services';
import { Incidents, IncidentDetail } from './pages/Incidents'; import { Investigations, InvestigationDetail } from './pages/Investigations';
import Audit from './pages/Audit'; import Reliability from './pages/Reliability'; import SettingsPage from './pages/Settings';
import Login from './pages/Login';

const NAV = [['/console', 'Console', MessageSquare], ['/overview', 'Overview', LayoutGrid], ['/services', 'Service Health', Server], ['/incidents', 'Incidents & Alerts', Bell], ['/investigations', 'Investigation History', History], ['/audit', 'Audit Trail', ScrollText], ['/reliability-cost', 'Reliability & Cost', TrendingDown], ['/settings', 'Settings & Integrations', Settings]] as const;

function Shell({ children }: any) {
  const s = useStore(); const auth = useAuth(); const loc = useLocation(); const nav = useNavigate(); const col = s.sidebar === 'collapsed';
  const [open, setOpen] = useState(false); const [q, setQ] = useState(''); const [menu, setMenu] = useState(''); const inp = useRef<HTMLInputElement>(null); const navRef = useRef<HTMLElement>(null); const [ind, setInd] = useState<{ y: number; on: boolean }>({ y: 0, on: false });
  useLayoutEffect(() => { const el = navRef.current?.querySelector<HTMLElement>('.nav.active'); setInd(el ? { y: el.offsetTop + (el.offsetHeight - 20) / 2, on: true } : { y: 0, on: false }); }, [loc.pathname, col]);
  const getComponentStatus = (name: string) => {
    if (s.health.status === 'loading' || s.health.status === 'idle') {
      return { status: 'checking', reason: 'Checking component status.', checkedAt: '' };
    }
    if (s.health.status === 'error') {
      return {
        status: 'unknown',
        reason: s.health.error || 'Component status could not be established.',
        checkedAt: '',
      };
    }
    const data = s.health.data;
    const components = data?.components;
    const component = components && typeof components === 'object'
      ? (components as Record<string, unknown>)[name] : null;
    if (!component || typeof component !== 'object') {
      return { status: 'unknown', reason: 'Component status was not reported.', checkedAt: '' };
    }
    const result = component as Record<string, unknown>;
    const validStatuses = ['healthy', 'degraded', 'unhealthy', 'unknown'];
    return {
      status: typeof result.status === 'string' && validStatuses.includes(result.status)
        ? result.status : 'unknown',
      reason: typeof result.reason === 'string'
        ? result.reason : 'Component status was not reported.',
      checkedAt: typeof result.checked_at === 'string' ? result.checked_at : '',
    };
  };
  const apiStatus = getComponentStatus('api');
  const apiUp = apiStatus.status === 'healthy';
  const apiLabel = apiStatus.status === 'checking' ? 'Loading' : apiStatus.status;
  const incidents = s.incidents.data || [];
  const audits = s.audit.data || [];
  useEffect(() => {
    void refresh('health');
    const interval = window.setInterval(() => void refresh('health'), 60_000);
    return () => window.clearInterval(interval);
  }, []);
  useEffect(() => {
    if (!auth) return;
    const refreshLifecycle = async (refreshApprovalList = false) => {
      if (refreshApprovalList) await refresh('approvals');
      await Promise.all([
        refresh('lifecycleCounts'),
        refresh('incidents'),
        refresh('audit'),
      ]);
    };
    void Promise.all([
      refresh('incidents'),
      refresh('services'),
      refresh('audit'),
      refresh('metrics'),
      refresh('cost'),
      refresh('monitor'),
      refreshLifecycle(true),
    ]);
    const interval = window.setInterval(() => {
      void refreshLifecycle();
    }, 30_000);
    return () => window.clearInterval(interval);
  }, [auth]);
  useEffect(() => { setOpen(false); setMenu(''); }, [loc.pathname]);
  useEffect(() => { const h = (e: KeyboardEvent) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); inp.current?.focus(); } }; window.addEventListener('keydown', h); return () => window.removeEventListener('keydown', h); }, []);
  // Match the Incidents page's default four-application-service scope. The backend
  // lifecycle count also includes Kubernetes-lab records hidden from that view.
  const openList = openIncidentsOf(incidents);
  const active = s.incidents.status === 'ready' ? openList.length : null;
  const badge = (path: string): [number | null, string] => path === '/incidents'
    ? [active, ''] : [0, ''];
  const ql = q.toLowerCase();
  const res = q ? [
    ...SERVICES.filter(service => service.includes(ql)).map(service => ['Service', service, `/services/${service}`]),
    ...incidents.filter(item => `${item.incident_id || ''} ${item.message || ''} ${item.service || ''}`.toLowerCase().includes(ql))
      .map(item => ['Incident', `${item.incident_id} ${item.message || ''}`, `/incidents/${item.incident_id}`]),
    ...audits.filter(item => `${item.event_type || ''} ${item.resource || ''} ${item.actor || ''}`.toLowerCase().includes(ql)).slice(0, 4)
      .map(item => ['Audit', `${item.event_type} · ${item.resource || ''}`, `/audit?q=${encodeURIComponent(String(item.resource || ''))}`]),
  ].slice(0, 12) : [];
  const name = auth ? 'Authenticated operator' : 'Signed out'; const ini = auth ? (String(s.operatorId || 'op').split(/[^A-Za-z0-9]+/).filter(Boolean).slice(0, 2).map(p => p[0]).join('').toUpperCase() || 'OP') : '—';
  return <div className="app">
    {open && <div className="overlay" style={{ zIndex: 80 }} onClick={() => setOpen(false)} />}
    <aside className={`side ${col ? 'collapsed' : ''} ${open ? 'open' : ''}`} aria-label="Primary">
      <div className="brand"><Logo small={col} /></div>
      <nav ref={navRef} className="navwrap" aria-busy={s.lifecycleCounts.status === 'loading'}><span className={`navind ${ind.on ? 'on' : ''}`} style={{ transform: `translateY(${ind.y}px)` }} aria-hidden />{NAV.map(([p, l, Ic]) => { const [n, w] = badge(p); const countTitle = p === '/incidents' ? 'active incidents' : ''; return <NavLink key={p} to={p} className={({ isActive }) => `nav ${isActive ? 'active' : ''}`} aria-label={l}><Ic size={18} /><span className="lbl">{l}</span>{n !== null && n > 0 && <span className={`cnt ${w}`} title={`${n} ${countTitle}`} aria-label={`${n} ${countTitle}`}>{n}</span>}<span className="tip">{l}{countTitle ? ` · ${countTitle}` : ''}</span></NavLink>; })}</nav>
      {s.lifecycleCounts.status === 'error' && <div className="tiny err" role="status">Sidebar counts unavailable: {s.lifecycleCounts.error || 'The request failed.'}</div>}
      <div className="sysbox"><b>System Status</b>
        {[
          ['api', 'API (FastAPI)'],
          ['prometheus', 'Prometheus'],
          ['kubernetes_lab', 'Kubernetes (Lab)'],
          ['database', 'Database'],
        ].map(([key, name]) => {
          const component = getComponentStatus(key);
          const statusLabel = component.status === 'checking'
            ? 'Loading' : component.status.charAt(0).toUpperCase() + component.status.slice(1);
          const detail = component.checkedAt
            ? `${component.reason} Checked at ${component.checkedAt}.`
            : component.reason;
          const badgeStatus = component.status === 'checking' ||
            component.status === 'unknown' ? 'pending' : component.status;
          return <div className="sys" key={key} title={detail}>
            <span className="row"><span className={`dot ${component.status === 'healthy' ? 'live' : 'idle'}`} />{name}</span>
            <span
              className={`badge b-${badgeStatus}`}
              aria-label={`${name} status: ${statusLabel}. ${detail}`}
              title={detail}
            >{statusLabel}</span>
          </div>;
        })}
        {s.health.error && <span className="tiny err">{s.health.error}</span>}</div>
      <button className="nav collapse-btn" aria-label={col ? 'Expand sidebar' : 'Collapse sidebar'} onClick={() => set({ sidebar: col ? 'expanded' : 'collapsed' })}>{col ? <ChevronsRight size={18} /> : <ChevronsLeft size={18} />}<span className="tip">Expand sidebar</span></button>
    </aside>
    <div className="main">
      <header className="hdr">
        <button className="icon mob" aria-label="Open navigation" onClick={() => setOpen(!open)}><Menu size={20} /></button>
        <div className="search"><Search size={16} className="sI" /><input ref={inp} aria-label="Global search" placeholder="Search services, incidents, investigations…" value={q} onChange={e => setQ(e.target.value)} onBlur={() => setTimeout(() => setQ(''), 200)} /><kbd>Ctrl K</kbd>
          {q && <div className="results">{res.length ? res.map(([k, t, to]: any, i) => <button key={i} onClick={() => { nav(to); setQ(''); }}><span className="muted">{k} · </span>{t}</button>) : <div className="pad muted">No results for “{q}”</div>}</div>}</div>
        <span className="spacer" />
        <span className="pill hide-m" title="FastAPI health endpoint"><span className={`dot ${apiUp ? 'live' : ''}`} style={apiUp ? undefined : { background: 'var(--cr)' }} /><span className="stl">{apiUp ? 'Connected' : apiStatus.status === 'checking' ? 'Checking' : apiStatus.status === 'unknown' ? 'Unknown' : 'Disconnected'}</span></span>
        {s.permissions.includes('read') && <button className="btn primary" onClick={() => nav('/console')}><Plus size={15} /><span className="stl">New Investigation</span></button>}
        <div style={{ position: 'relative' }}><button className={`icon ${active && active > 0 ? 'ring' : ''}`} aria-label="Open incidents" onClick={() => { const next = menu === 'n' ? '' : 'n'; setMenu(next); if (next === 'n') void refresh('incidents'); }}><Bell size={20} />{active !== null && active > 0 && <span className="cnt" aria-label={`${active} active incidents`} style={{ position: 'absolute', top: -2, right: -4, minWidth: 16, height: 16, lineHeight: '16px', fontSize: 10, padding: '0 4px' }}>{active}</span>}</button>
          {menu === 'n' && <div className="menu">{s.incidents.status === 'error' ? <div className="pad muted">{s.incidents.error}</div> : s.incidents.status === 'loading' || s.incidents.status === 'idle' ? <div className="pad muted">Loading incidents…</div> : openList.map(i => <button key={String(i.incident_id)} onClick={() => { nav(`/incidents/${i.incident_id}`); setMenu(''); }}>{String(i.incident_id)} · {String(i.incident_type)}<div className="muted tiny">{String(i.service)} · {String(i.severity)}</div></button>)}{s.incidents.status === 'ready' && openList.length === 0 && <div className="pad muted">No open incidents.</div>}</div>}</div>
        <div style={{ position: 'relative' }}><button className="row gap" style={{ background: 'none', border: 0, padding: 0 }} aria-label="Account menu" onClick={() => setMenu(menu === 'u' ? '' : 'u')}><span className="avatar">{ini}</span><ChevronDown size={14} /></button>
          {menu === 'u' && <div className="menu acct">
            <img className="acct-mark" src={logo} alt="" aria-hidden />
            <div className="acct-top"><span className="acct-brand">Kybernaut</span><span className="avatar lg">{ini}</span></div>
            <div className="acct-name"><b>{s.operatorId || name}</b><span>Authenticated operator</span></div>
            <dl className="acct-kv"><dt>Workspace</dt><dd>Kybernaut</dd><dt>Data source</dt><dd>FastAPI backend</dd><dt>Permissions</dt><dd>{s.permissions.join(', ') || 'None'}</dd></dl>
            <div className="acct-act"><button onClick={() => { nav('/settings'); setMenu(''); }}><UserRound size={15} />Profile &amp; settings</button>
            <button className="out" onClick={() => { signOut(); toast('Signed out'); nav('/login', { replace: true }); }}><LogOut size={15} />Log out</button></div></div>}</div>
      </header>
      <main className="content"><div key={loc.pathname} className="topbar" /><div key={loc.pathname + 'r'} className="route">{children}</div></main>
    </div><Toaster /></div>;
}
const NotFound = () => <Page title="Page not found" sub="The page you requested does not exist."><Link className="btn primary" style={{ width: 'fit-content' }} to="/console">Back to Console</Link></Page>;
function Guard({ children }: any) { const a = useAuth(); const l = useLocation(); return a ? children : <Navigate to="/login" state={{ from: l.pathname + l.search }} replace />; }
function Inner() {
  return <Shell><Routes><Route path="/" element={<Navigate to="/console" replace />} />
    <Route path="/console" element={<Console />} /><Route path="/overview" element={<Overview />} />
    <Route path="/services" element={<Services />} /><Route path="/services/:serviceId" element={<ServiceDetail />} />
    <Route path="/incidents" element={<Incidents />} /><Route path="/incidents/:incidentId" element={<IncidentDetail />} />
    <Route path="/investigations" element={<Investigations />} /><Route path="/investigations/:investigationId" element={<InvestigationDetail />} />
    <Route path="/approvals" element={<Navigate to="/incidents" replace />} /><Route path="/audit" element={<Audit />} /><Route path="/reliability-cost" element={<Reliability />} /><Route path="/settings/*" element={<SettingsPage />} />
    <Route path="*" element={<NotFound />} /></Routes></Shell>;
}
export default function App() { return <Routes><Route path="/login" element={<Login />} /><Route path="/*" element={<Guard><Inner /></Guard>} /></Routes>; }