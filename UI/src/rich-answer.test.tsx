import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AssistBubble } from './pages/RemediationCards';

afterEach(cleanup);

describe('structured assistant answers', () => {
  it('renders a lead sentence, headings and bullet points instead of one raw block', () => {
    const answer = [
      'inc-1 is a **request failure** on **inventory-service**.',
      '## Cause',
      '- A deliberate **local simulation** injected the fault.',
      '## Not recorded',
      '- No policy outcome is recorded.',
    ].join('\n');
    const { container } = render(<MemoryRouter>
      <AssistBubble result={{ answer, citations: ['incident'], llm_used: false }} />
    </MemoryRouter>);

    expect(screen.getByText('Cause')).toBeTruthy();
    expect(screen.getByText('Not recorded')).toBeTruthy();
    expect(container.querySelectorAll('.rich li')).toHaveLength(2);
    expect(container.querySelectorAll('.rich b').length).toBeGreaterThanOrEqual(3);
    expect(container.textContent).not.toContain('**');
    expect(container.textContent).not.toContain('## ');
  });
});