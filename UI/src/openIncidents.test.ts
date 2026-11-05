import { describe, expect, it } from 'vitest';
import { openIncidentsOf } from './openIncidents';

const row = (id: string, status: string, extra: Record<string, unknown> = {}) =>
  ({ incident_id: id, status, service: 'order-service', ...extra });

describe('openIncidentsOf', () => {
  it('keeps only unresolved incidents and drops resolved and demo records', () => {
    const list = openIncidentsOf([
      row('a', 'OPEN'),
      row('b', 'RESOLVED'),
      row('c', 'REMEDIATING'),
      row('d', 'open'),
      row('e', 'OPEN', { source: 'demo' }),
      row('f', 'OPEN', { service: 'kubernetes-lab' }),
    ]);
    expect(list.map(item => item.incident_id)).toEqual(['a', 'c', 'd']);
  });

  it('handles missing data', () => {
    expect(openIncidentsOf(null)).toEqual([]);
    expect(openIncidentsOf(undefined)).toEqual([]);
  });

  it('orders open incidents critical, then high, then medium, then low', () => {
    const list = openIncidentsOf([
      row('m1', 'OPEN', { severity: 'medium' }),
      row('h1', 'OPEN', { severity: 'high' }),
      row('c1', 'OPEN', { severity: 'critical' }),
      row('l1', 'OPEN', { severity: 'low' }),
      row('h2', 'REMEDIATING', { severity: 'High' }),
      row('c2', 'OPEN', { severity: 'critical' }),
      row('x1', 'OPEN'),
    ]);
    expect(list.map(item => item.incident_id)).toEqual(['c1', 'c2', 'h1', 'h2', 'm1', 'l1', 'x1']);
  });
});