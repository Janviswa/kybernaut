import { ReactNode, useState } from 'react';
import { ArrowLeft, ChevronRight } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Badge } from './ui';
import { fmt } from './data';

/** Back arrow: goes to the previous page, or to `fallback` on a fresh tab. */
export function BackButton({ fallback = '/overview', label = 'Back' }: { fallback?: string; label?: string }) {
  const nav = useNavigate();
  const loc = useLocation();
  return <button className="back" type="button" aria-label={label} title={label}
    onClick={() => (loc.key !== 'default' ? nav(-1) : nav(fallback))}>
    <ArrowLeft size={16} /><span>{label}</span>
  </button>;
}

export type TimelineEvent = {
  id: string; title: string; time?: string | null; actor?: string | null;
  outcome?: string | null; detail?: string | null; resource?: string | null;
};

const toneFor = (event: TimelineEvent) => {
  const value = `${event.title} ${event.outcome || ''}`.toLowerCase();
  if (/fail|error|reject|denied|block|invalid|unavailable/.test(value)) return 'bad';
  if (/approved|success|complete|resolved|verified|executed|recovered/.test(value)) return 'good';
  if (/pending|request|propos|await|expire/.test(value)) return 'warn';
  return 'info';
};

/** Animated vertical timeline: the rail draws in, then nodes pop in one by one. */
export function AnimatedTimeline({ events, empty = 'No events recorded.' }: { events: TimelineEvent[]; empty?: string }) {
  if (!events.length) return <div className="empty">{empty}</div>;
  return <ol className="atl">
    {events.map((event, index) => <li key={event.id} className={`atl-${toneFor(event)}`} style={{ animationDelay: `${Math.min(index, 14) * 70}ms` }}>
      <span className="atl-dot" aria-hidden />
      <div className="atl-card">
        <div className="row between gap"><b>{event.title.replace(/_/g, ' ')}</b>
          {event.outcome && <Badge v={toneFor(event) === 'bad' ? 'failed' : toneFor(event) === 'good' ? 'success' : toneFor(event) === 'warn' ? 'pending' : 'info'}>{event.outcome}</Badge>}</div>
        <span className="tiny muted">{fmt(event.time)}{event.actor ? ` · ${event.actor}` : ''}{event.resource ? ` · ${event.resource}` : ''}</span>
        {event.detail && <p className="tiny">{event.detail}</p>}
      </div>
    </li>)}
  </ol>;
}

export const eventsFromAudit = (rows: Record<string, unknown>[]): TimelineEvent[] =>
  rows.map((row, index) => ({
    id: String(row.id ?? row.event_id ?? `${row.timestamp}-${index}`),
    title: String(row.event_type ?? 'event'),
    time: typeof row.timestamp === 'string' ? row.timestamp : null,
    actor: row.actor ? String(row.actor) : null,
    outcome: row.outcome ? String(row.outcome) : null,
    resource: row.resource ? String(row.resource) : null,
    detail: row.summary ? String(row.summary) : null,
  }));

/** Collapsible tree: incident -> its audit events, each branch animated. */
export function AuditTree({ rows, onSelect }: { rows: Record<string, unknown>[]; onSelect?: (row: Record<string, unknown>) => void }) {
  const groups = new Map<string, Record<string, unknown>[]>();
  rows.forEach(row => {
    const key = String(row.incident_ref || row.resource || 'general');
    groups.set(key, [...(groups.get(key) || []), row]);
  });
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const entries = [...groups.entries()];
  if (!entries.length) return <div className="empty">No audit events returned.</div>;
  return <ul className="atree">
    {entries.map(([key, items], gi) => {
      const expanded = open[key] ?? gi < 3;
      const sorted = [...items].sort((a, b) => String(a.timestamp).localeCompare(String(b.timestamp)));
      return <li key={key} style={{ animationDelay: `${Math.min(gi, 10) * 80}ms` }}>
        <button className="atree-root" aria-expanded={expanded} onClick={() => setOpen(o => ({ ...o, [key]: !expanded }))}>
          <ChevronRight size={16} className={expanded ? 'rot' : ''} />
          <b>{key}</b><span className="tiny muted">{items.length} event{items.length === 1 ? '' : 's'} · latest {fmt(String(sorted[sorted.length - 1].timestamp))}</span>
        </button>
        {expanded && <div className="atree-kids">
          {sorted.map((row, index) => {
            const event = eventsFromAudit([row])[0];
            return <div key={event.id} className={`atree-kid atl-${toneFor(event)}`} style={{ animationDelay: `${Math.min(index, 12) * 60}ms` }}
              role={onSelect ? 'button' : undefined} tabIndex={onSelect ? 0 : undefined}
              onClick={() => onSelect?.(row)} onKeyDown={e => e.key === 'Enter' && onSelect?.(row)}>
              <span className="atl-dot" aria-hidden />
              <div><b>{event.title.replace(/_/g, ' ')}</b> <span className="tiny muted">{fmt(event.time)} · {event.actor || 'unknown'}</span>
                {event.outcome && <span className="tiny"> · {event.outcome}</span>}
                {event.detail && <p className="tiny muted">{event.detail}</p>}</div>
            </div>;
          })}
        </div>}
      </li>;
    })}
  </ul>;
}

export const Scrollbox = ({ title, actions, children }: { title: ReactNode; actions?: ReactNode; children: ReactNode }) =>
  <section className="sbox"><header className="row between gap"><h3>{title}</h3>{actions}</header><div className="sbox-body">{children}</div></section>;