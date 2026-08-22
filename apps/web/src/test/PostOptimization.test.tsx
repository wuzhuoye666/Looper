import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';

const experiment = {
  id: 'exp-done', name: '完成的压缩测试', status: 'completed', mode: 'optimization',
  benchmarkId: 'looper.demo.compression', benchmarkName: 'Deterministic compression loop',
  targetId: 'local', targetName: '本机执行目标', progress: 100, attempts: 3, maxAttempts: 3,
  objective: 'throughput_mib_s', bestScore: 100, baselineScore: 100,
  createdAt: '2026-08-22T00:00:00Z', updatedAt: '2026-08-22T00:05:00Z', evaluations: [], config: {},
};

const action = {
  id: 'larger-compression-chunks', label: '增大压缩分块并复测', risk: 'low',
  applyMode: 'benchmark-parameter', parameter: 'chunk_size', value: 65536,
  before: 16384, after: 65536, minimumImprovementRatio: 0.05,
  description: '只调整一个低风险 Benchmark 参数。',
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>
    <MemoryRouter initialEntries={['/experiments/exp-done']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <App />
    </MemoryRouter>
  </QueryClientProvider>);
}

describe('Benchmark 完成后的优化复测', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      let data: unknown = {};
      if (url.endsWith('/experiments/exp-done/post-optimization') && init?.method === 'POST') {
        data = {
          eligible: false, status: 'retesting', reason: '优化候选已经生成，正在使用同一 Benchmark 复测。', action,
          baselineParameters: { compression_level: 6, chunk_size: 16384 },
          followUpExperiment: { ...experiment, id: 'exp-retest', name: '完成的压缩测试 · 优化复测', status: 'queued' },
        };
      } else if (url.endsWith('/experiments/exp-done/post-optimization')) {
        data = {
          eligible: true, status: 'ready', reason: '已找到一个未测试的低风险候选。', action,
          baselineParameters: { compression_level: 6, chunk_size: 16384 },
        };
      } else if (url.endsWith('/experiments/exp-done')) {
        data = experiment;
      }
      return new Response(JSON.stringify(data), {
        status: init?.method === 'POST' ? 201 : 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }));
  });

  it('完成后显示白名单动作按钮并创建复测实验', async () => {
    renderPage();

    expect(await screen.findByRole('heading', { name: '完成的压缩测试' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /优化并重新测试/ })).toBeEnabled();
    expect(screen.getByText('chunk_size: 16384 → 65536')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: /优化并重新测试/ }));

    expect(await screen.findByText('正在复测')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '查看复测实验' })).toHaveAttribute(
      'href', '/experiments/exp-retest',
    );
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === 'POST');
      expect(String(call?.[0])).toContain('/experiments/exp-done/post-optimization');
    });
  });
});
