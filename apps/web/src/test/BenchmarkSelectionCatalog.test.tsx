import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CreateExperimentPage } from '../pages/CreateExperimentPage';

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>
    <MemoryRouter><CreateExperimentPage /></MemoryRouter>
  </QueryClientProvider>);
}

describe('选型研究 Benchmark 目录', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.endsWith('/benchmarks') ? { items: [
        {
          id: 'registered.adapter', key: 'registered.adapter@1.0.0',
          name: 'Registered Adapter', version: '1.0.0', category: 'database',
          selectionReady: true, runnable: true, tags: ['python'],
          scenario: {
            id: 'registered.adapter', name: 'Registered Adapter',
            decision_question: 'Which target performs better?', user_value: 'Target selection',
            workload_class: 'database', topology: 'single-node', roles: [],
            primary_metric: 'score', slo_gates: [],
          },
        },
        {
          id: 'incomplete.adapter', key: 'incomplete.adapter@1.0.0',
          name: 'Incomplete Adapter', version: '1.0.0', category: 'unclassified',
          selectionReady: false, runnable: false,
        },
      ] } : { items: [] };
      return new Response(JSON.stringify(data), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }));
  });

  it('显示新注册 Benchmark，并标记缺少选型合同的项目', async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText('研究名称 *'), { target: { value: '新套件选型' } });
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));

    expect(await screen.findByRole('option', { name: 'Registered Adapter · 1.0.0' })).toBeEnabled();
    expect(screen.getByRole('option', { name: /Incomplete Adapter.*缺少选型合同/ })).toBeDisabled();
  });
});
