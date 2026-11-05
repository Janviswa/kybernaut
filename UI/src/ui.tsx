import { useEffect, useLayoutEffect, useRef, useState, ReactNode } from 'react';
import { Copy, X, ChevronLeft, ChevronRight, ArrowUpDown } from 'lucide-react';
import { toast } from './store';
// Shared chart theme (white & teal) so every page's charts match.
export const CH = {
  axis: '#6B8789', grid: '#E4EEEE', teal: '#0E8C8F', tealSoft: '#0E8C8F26', mint: '#7CD1CB', warn: '#D97706',
  tip: { contentStyle: { background: '#fff', border: '1px solid #DCE8E8', borderRadius: 10, boxShadow: '0 8px 24px #0a2a2e1f', color: '#0A2A2E', fontSize: 12 }, cursor: { stroke: '#C6DADA' } },
  anim: { animationDuration: 900, animationEasing: 'ease-out' as const },
};
export const Badge = ({ v, children }: { v: string; children?: ReactNode }) => <span className={`badge b-${v}`}>{children ?? v}</span>;
export const CopyBtn = ({ text }: { text: string }) => <button className="icon" aria-label={`Copy ${text}`} onClick={() => { navigator.clipboard?.writeText(text); toast('Copied to clipboard'); }}><Copy size={14} /></button>;
export const Modal = ({ title, onClose, children, foot }: any) => {
  useEffect(() => { const h = (e: KeyboardEvent) => e.key === 'Escape' && onClose(); window.addEventListener('keydown', h); return () => window.removeEventListener('keydown', h); }, []);
  return (
  <div className="overlay" onClick={onClose}><div className="modal" role="dialog" aria-label={title} onClick={e => e.stopPropagation()}>
    <div className="row between"><h3>{title}</h3><button className="icon" aria-label="Close" onClick={onClose}><X size={16} /></button></div>{children}{foot && <div className="row end gap">{foot}</div>}</div></div>); };
export const Confirm = ({ title, text, reason, onOk, onClose, label = 'Confirm' }: any) => {
  const [r, setR] = useState('');
  return <Modal title={title} onClose={onClose} foot={<><button className="btn" onClick={onClose}>Cancel</button><button className="btn primary" disabled={reason && !r.trim()} onClick={() => { onOk(r); onClose(); }}>{label}</button></>}>
    <p className="muted">{text}</p>{reason && <textarea placeholder="Reason (required)" value={r} onChange={e => setR(e.target.value)} />}</Modal>;
};
let toastSequence = 0;
export const Toaster = () => {
  const [t, setT] = useState<any[]>([]);
  useEffect(() => { const h = (e: any) => { const id = ++toastSequence; setT(x => [...x, { id, ...e.detail }]); setTimeout(() => setT(x => x.filter(i => i.id !== id)), 3500); }; window.addEventListener('kb-toast', h); return () => window.removeEventListener('kb-toast', h); }, []);
  return <div className="toasts" aria-live="polite">{t.map(i => <div key={i.id} className={`toast ${i.kind}`}>{i.m}</div>)}</div>;
};
export const Empty = ({ text }: { text: string }) => <div className="empty">{text}</div>;
export const ResourceMessage = ({ resource, retry, empty }: {
  resource: { status: string; error: string | null };
  retry?: () => void;
  empty?: string;
}) => {
  if (resource.status === 'loading' || resource.status === 'idle') {
    return <div className="panel muted" role="status"><span className="spinner" /> Loading backend data…</div>;
  }
  if (resource.status === 'unavailable') {
    return <div className="panel muted" role="status">{resource.error || 'This data is not available from the backend.'}</div>;
  }
  if (resource.status === 'error') {
    return <div className="panel" role="alert"><p className="err">{resource.error || 'The backend request failed.'}</p>{retry && <button className="btn" onClick={retry}>Retry</button>}</div>;
  }
  if (empty) return <Empty text={empty} />;
  return null;
};
export const Page = ({ title, sub, actions, children }: any) => <div className="page"><div className="row between wrap"><div><h1>{title}</h1>{sub && <p className="muted">{sub}</p>}</div><div className="row gap print-hide">{actions}</div></div>{children}</div>;
export function DataTable({ cols, rows, onRow, size = 8, empty = 'No records match the current filters.' }: any) {
  const [sk, setSk] = useState<string>(''); const [asc, setAsc] = useState(true); const [p, setP] = useState(0);
  const sorted = [...rows].sort((a, b) => (sk ? (a[sk] > b[sk] ? 1 : a[sk] < b[sk] ? -1 : 0) * (asc ? 1 : -1) : 0));
  const pages = Math.max(1, Math.ceil(sorted.length / size)); const pg = Math.min(p, pages - 1);
  if (!rows.length) return <Empty text={empty} />;
  return <div className="panel noPad"><div className="scroll"><table><thead><tr>{cols.map((c: any) => <th key={c.k} className={c.cls}><button className="th" onClick={() => { setAsc(sk === c.k ? !asc : true); setSk(c.k); }}>{c.h}<ArrowUpDown size={11} /></button></th>)}</tr></thead>
    <tbody>{sorted.slice(pg * size, pg * size + size).map((r: any, i: number) => <tr key={r.id ?? r.service ?? r.approval_id ?? `${pg}-${i}`} className={onRow ? 'click' : ''} tabIndex={onRow ? 0 : undefined} onKeyDown={e => e.key === 'Enter' && onRow?.(r)} onClick={() => onRow?.(r)}>{cols.map((c: any) => <td key={c.k} className={c.cls}>{c.r ? c.r(r) : r[c.k]}</td>)}</tr>)}</tbody></table></div>
    <div className="row between pad"><span className="muted">{rows.length} records</span><div className="row gap"><button className="icon" aria-label="Previous page" disabled={pg === 0} onClick={() => setP(pg - 1)}><ChevronLeft size={16} /></button><span>{pg + 1} / {pages}</span><button className="icon" aria-label="Next page" disabled={pg >= pages - 1} onClick={() => setP(pg + 1)}><ChevronRight size={16} /></button></div></div></div>;
}
export const Tabs = ({ tabs, v, set }: any) => {
  const ref = useRef<HTMLDivElement>(null); const [ink, setInk] = useState({ l: 0, w: 0, go: false });
  useLayoutEffect(() => { const el = ref.current?.querySelector<HTMLElement>('[aria-selected="true"]'); if (el) setInk(p => ({ l: el.offsetLeft, w: el.offsetWidth, go: p.w > 0 })); }, [v, tabs.length]);
  return <div className="tabs" role="tablist" ref={ref}>{tabs.map((t: any) => { const [k, l] = Array.isArray(t) ? t : [t, t]; return <button key={k} role="tab" aria-selected={v === k} className={v === k ? 'on' : ''} onClick={() => set(k)}>{l}</button>; })}
    <span className={`ink ${ink.go ? 'go' : ''}`} style={{ transform: `translateX(${ink.l}px)`, width: ink.w }} aria-hidden /></div>;
};
export const Kpi = ({ label, value, onClick, tone }: any) => {
  const [n, setN] = useState(typeof value === 'number' ? 0 : value);
  useEffect(() => { if (typeof value !== 'number') { setN(value); return; } let f = 0; const t0 = performance.now(); const tick = (t: number) => { const p = Math.min(1, (t - t0) / 700); setN(Math.round(value * (1 - Math.pow(1 - p, 3)))); if (p < 1) f = requestAnimationFrame(tick); }; f = requestAnimationFrame(tick); return () => cancelAnimationFrame(f); }, [value]);
  return <button className={`kpi ${tone || ''}`} onClick={onClick}><span className="muted">{label}</span><b>{n}</b></button>;
};
/** Crisp vector mark (three stacked layers). `light` is for dark tiles, `brand` for light backgrounds. */
export const LogoMark = ({ size = 26, tone = 'light', title }: { size?: number; tone?: 'light' | 'brand'; title?: string }) => {
  const [top, mid, low] = tone === 'light' ? ['#EFFFFD', '#8CE8DF', '#34B8B0'] : ['#34C9BE', '#14968F', '#0B666B'];
  const layer = (y: number, fill: string) => <path d={`M32 ${y} 55 ${y + 12.5} 32 ${y + 25} 9 ${y + 12.5}Z`} fill={fill} stroke={fill} strokeWidth={5} strokeLinejoin="round" />;
  return <svg width={size} height={size} viewBox="0 0 64 64" role={title ? 'img' : undefined} aria-label={title} aria-hidden={title ? undefined : true} style={{ display: 'block' }}>
    {layer(31, low)}{layer(19, mid)}{layer(7, top)}
  </svg>;
};
export const Logo = ({ small }: { small?: boolean }) => (
  <div className="row gap"><span className="logobox"><LogoMark size={26} title="Kybernaut" /></span>
    {!small && <div><b className="wm">Kybernaut</b><div className="muted tiny">Agentic AIOps platform</div></div>}</div>);