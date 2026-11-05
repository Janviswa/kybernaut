import { describe, expect, it } from 'vitest';
import { classifyConsoleIntent } from './consoleIntents';

describe('natural-language Console intent classification', () => {
  it.each([
    ['Check service health', 'health'],
    ['Is the order service healthy?', 'health', 'order-service'],
    ['How is payment-service doing?', 'ambiguous', 'payment-service'],
    ['Why is payment service slow?', 'metrics', 'payment-service'],
    ['What happened?', 'incident_question'],
    ['How the memory got leaked and how it got fixed?', 'incident_question'],
    ['Why was remediation not executed for this incident?', 'incident_question'],
    ['Summarize the RCA', 'incident_question'],
    ['Show CPU and memory usage', 'metrics'],
    ['Show current telemetry for payment-service only', 'metrics', 'payment-service'],
    ['Which service currently has the highest P95 latency?', 'highest_p95'],
    ['What is the current cluster status?', 'kubernetes'],
    ['Show the audit trail for this incident', 'audit'],
    ['Investigate incident inc-123.', 'incident', undefined, 'inc-123'],
    ['Show the latest alerts', 'incident_list'],
    ['What remediation is recommended for user-service?', 'recommendation', 'user-service'],
    ['What policy was recommended for incident inc-123?', 'recommendation', undefined, 'inc-123'],
    ['What can Kybernaut do?', 'capabilities'],
    ['Search application logs for timeouts', 'unsupported'],
    ['Restart order-service', 'unsupported', 'order-service'],
  ])('%s routes to %s', (query, kind, service, incidentId) => {
    expect(classifyConsoleIntent(query)).toEqual({
      kind,
      ...(service ? { service } : {}),
      ...(incidentId ? { incidentId } : {}),
    });
  });

  it.each([
    ['Last 5 audit only', 5],
    ['Show 5 audit events', 5],
    ['Latest 10 audit entries', 10],
    ['Show the last 3 audit records', 3],
  ])('preserves the requested audit count in "%s"', (query, count) => {
    expect(classifyConsoleIntent(query)).toEqual({ kind: 'audit', count });
  });

  it('routes service availability to the configured-service list, not health', () => {
    expect(classifyConsoleIntent('What services are available?'))
      .toEqual({ kind: 'service_list' });
  });

  it('keeps all-service health separate from service-specific health', () => {
    expect(classifyConsoleIntent('Check all service health'))
      .toEqual({ kind: 'health_all' });
    expect(classifyConsoleIntent('Check health of unknown-service'))
      .toEqual({ kind: 'health', service: 'unknown-service' });
  });

  it('classifies incident timelines before generic incident lookup', () => {
    expect(classifyConsoleIntent('Show the timeline for incident inc-3b8048c22d8e'))
      .toEqual({ kind: 'timeline', incidentId: 'inc-3b8048c22d8e' });
  });

  it('preserves incident IDs for incident-specific audit requests', () => {
    expect(classifyConsoleIntent('Show the audit trail for incident inc-3b8048c22d8e'))
      .toEqual({ kind: 'audit', incidentId: 'inc-3b8048c22d8e', count: undefined });
  });

  it('rejects direct remediation requests as unsupported', () => {
    expect(classifyConsoleIntent('Remediate payment-service immediately without approval'))
      .toEqual({ kind: 'unsupported', service: 'payment-service' });
  });
});
