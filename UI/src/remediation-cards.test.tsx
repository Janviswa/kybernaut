import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AnswerSummary, AssistBubble, InvestigationBubbles, OptionBubbles, RemediationPanel, StepsBubble } from './pages/RemediationCards';

const result = {
  incident_id: 'inc-k8s-1', status: 'awaiting_approval', approval_id: 'apr-1',
  classification: { incident_type: 'disk_full', confidence: 0.8, evidence: ['Pod evicted: ephemeral storage'] },
  rca: { root_cause: 'Scratch volume exhausted', confidence: 0.7, evidence: ['evicted'], recommended_action: 'restart_pod' },
  proposal: { action: 'restart_pod', resource: 'sim-disk-fill-abc', rationale: 'Recreate the evicted pod' },
  policy_decision: { allowed: true, requires_approval: true, rules: [{ id: 'blast_radius_restart', passed: true, detail: 'ok' }] },
};

const view = (props: Partial<Parameters<typeof InvestigationBubbles>[0]> = {}) => render(
  <MemoryRouter><InvestigationBubbles result={result} approvals={[{ approval_id: 'apr-1', status: 'PENDING' }]}
    canDecide onExecute={vi.fn(async () => undefined)} {...props} /></MemoryRouter>);

describe('remediation suggestion bubbles', () => {
  afterEach(() => cleanup());

  it('renders the suggestion in its own bubble with an Execute button', () => {
    view();
    expect(screen.getByText('Remediation suggestion')).toBeTruthy();
    expect(screen.getByText(/Scratch volume exhausted/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Execute' })).toBeTruthy();
  });

  it('approves and executes from a single Execute click', async () => {
    const onExecute = vi.fn(async () => undefined);
    view({ onExecute });
    fireEvent.click(screen.getByRole('button', { name: 'Execute' }));
    await waitFor(() => expect(onExecute).toHaveBeenCalledWith(
      'apr-1', 'Approved from the Console Execute action.',
    ));
  });

  it('shows backend execution failures', async () => {
    const onExecute = vi.fn(async () => { throw new Error('Execution was blocked by backend policy'); });
    view({ onExecute });
    fireEvent.click(screen.getByRole('button', { name: 'Execute' }));

    expect(await screen.findByRole('alert')).toBeTruthy();
    expect(screen.getByText(/blocked by backend policy/)).toBeTruthy();
  });

  it('disables Execute without the remediation permission and when not pending', () => {
    view({ canDecide: false });
    expect((screen.getByRole('button', { name: 'Execute' }) as HTMLButtonElement).disabled).toBe(true);
    cleanup();
    view({ approvals: [{ approval_id: 'apr-1', status: 'APPROVED' }] });
    expect(screen.queryByRole('button', { name: 'Execute' })).toBeNull();
  });

  it('says so when the agent proposes no executable action', () => {
    view({ result: { ...result, proposal: null, approval_id: null, plan_note: 'no_automated_action' } });
    expect(screen.getByText('No automated remediation')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Execute' })).toBeNull();
  });
});

const options = {
  incident_type: 'pod_crash', live_evidence: true,
  classification: { confidence: 0.85, evidence: ['CrashLoopBackOff'] },
  options: [
    { option_id: 'action-restart_pod', kind: 'executable', executable: true, action: 'restart_pod', title: 'Recreate the affected pod (web-1)', rationale: 'clears a crash loop', risk: 'brief downtime', selected: true },
    { option_id: 'action-rollback_deployment', kind: 'executable', executable: false, action: 'rollback_deployment', title: 'Roll back (demo-web)', risk: 'reverts release', runbook_reason: 'minimum_ready_replicas_not_met' },
    { option_id: 'manual-1', kind: 'manual', executable: false, title: 'Read the previous logs', steps: ['kubectl logs --previous'], risk: 'Read-only.' },
  ],
};

describe('option bubbles, steps and assistant answers', () => {
  afterEach(() => cleanup());

  it('shows the action subject and dispatches Execute for a currently executable option', () => {
    const onExecute = vi.fn();
    render(<MemoryRouter><RemediationPanel approvals={[]} events={[]} policy={null} recs={[]}
      options={[{ kind: 'executable', executable: true, action: 'scale_deployment',
        title: 'Scale the deployment out by one replica (web)' }]}
      canRun busy={false} onExecute={onExecute} /></MemoryRouter>);

    expect(screen.getByText('Scale the deployment out by one replica (web)')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Execute' }));
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({ action: 'scale_deployment' }));
  });

  it('explains the incident-specific recovery in the Remediation tab', () => {
    render(<MemoryRouter><RemediationPanel approvals={[]} events={[]} policy={null} recs={[]}
      incidentType="request_failure"
      options={[{
        option_id: 'clear-local-simulation',
        kind: 'executable',
        executable: true,
        action: 'clear_local_simulation',
        resource: 'inventory-service',
        title: 'Clear the simulated fault on inventory-service',
        steps: ["Clear only this service's active simulator fault."],
        risk: 'Does not execute changes in Kubernetes.',
      }]} canRun resolved={false} onExecute={vi.fn()} /></MemoryRouter>);

    expect(screen.getByText('Stop the HTTP 500 request failures on inventory-service')).toBeTruthy();
    expect(screen.getByText(/stock and availability queries/)).toBeTruthy();
    expect(screen.getByText(/500 rate returns to zero/)).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/simulat|Kubernetes/i);
  });

  it('renders one bubble per option and only executable ones get an enabled Execute', () => {
    const onExecute = vi.fn();
    render(<MemoryRouter><OptionBubbles data={options} canRun resolved={false} busy={false} onExecute={onExecute} /></MemoryRouter>);

    expect(screen.getByText(/Option 1/)).toBeTruthy();
    expect(screen.getByText(/Option 3/)).toBeTruthy();
    const buttons = screen.getAllByRole('button', { name: 'Execute' }) as HTMLButtonElement[];
    expect(buttons).toHaveLength(2);
    expect(buttons[0].disabled).toBe(false);
    expect(buttons[1].disabled).toBe(true);
    fireEvent.click(buttons[0]);
    expect(onExecute).toHaveBeenCalledWith(expect.objectContaining({ action: 'restart_pod' }));
  });

  it('enables Execute for the guarded pod-crash command restore option', () => {
    const onExecute = vi.fn();
    render(<MemoryRouter><OptionBubbles data={{
      ...options,
      options: [{
        option_id: 'action-restore_command',
        kind: 'executable',
        executable: true,
        requires_approval: true,
        action: 'restore_command',
        title: 'Restore the registered known-good container command (sim-pod-crash)',
        selected: true,
      }],
    }} canRun resolved={false} busy={false} onExecute={onExecute} /></MemoryRouter>);

    const button = screen.getByRole('button', { name: 'Execute' }) as HTMLButtonElement;
    expect(button.disabled).toBe(false);
    fireEvent.click(button);
    expect(onExecute).toHaveBeenCalledWith(
      expect.objectContaining({ action: 'restore_command' }),
    );
  });

  it('disables options at the action limit and explains the policy block', () => {
    const data = {
      ...options,
      options: [{
        option_id: 'action-restore_command',
        kind: 'executable',
        executable: true,
        policy_available: false,
        policy_reason: 'action_claim_limit_reached',
        policy_detail: 'Action limit reached (5/5) for the last 3600 seconds.',
        action: 'restore_command',
        title: 'Stop the simulator memory writer',
      }],
    };
    render(<MemoryRouter><OptionBubbles data={data} canRun resolved={false} busy={false}
      onExecute={vi.fn()} /></MemoryRouter>);

    expect(screen.getByText('not available')).toBeTruthy();
    expect(screen.getByText(/Action limit reached \(5\/5\)/)).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Execute' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('disables every Execute once the incident is resolved', () => {
    render(<MemoryRouter><OptionBubbles data={options} canRun resolved busy={false} onExecute={vi.fn()} /></MemoryRouter>);
    (screen.getAllByRole('button', { name: 'Execute' }) as HTMLButtonElement[]).forEach(button => expect(button.disabled).toBe(true));
  });

  it('shows each step with its real status and detail', () => {
    render(<StepsBubble title="Executing…" steps={[
      { label: 'Recording your approval', status: 'done', detail: 'Approved' },
      { label: 'Running restart pod', status: 'active' },
      { label: 'Verifying recovery', status: 'pending' }]} />);

    expect(screen.getByText('Recording your approval')).toBeTruthy();
    expect(screen.getByText('Approved')).toBeTruthy();
    expect(document.querySelector('.step-active')).not.toBeNull();
  });

  it('labels whether an answer came from the model or only from records', () => {
    render(<AssistBubble result={{ answer: 'It is OPEN.', citations: ['incident'], llm_used: false, note: 'The language model is unavailable.' }} />);

    expect(screen.getByText('records only')).toBeTruthy();
    expect(screen.getByText('incident')).toBeTruthy();
  });

  it('turns a line-per-incident answer into a tidy list with links', () => {
    render(<MemoryRouter><AnswerSummary textValue={'2 incidents found.\ninc-aaa111 · memory_leak · user-service · medium · OPEN\ninc-bbb222 · pod_crash · demo-web · high · OPEN'} /></MemoryRouter>);

    const link = screen.getByRole('link', { name: 'inc-aaa111' });
    expect(link.getAttribute('href')).toBe('/console?incident=inc-aaa111');
    expect(document.querySelectorAll('.answerlist li')).toHaveLength(2);
  });
});