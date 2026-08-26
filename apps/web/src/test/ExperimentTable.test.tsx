import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { ExperimentTable } from '../components/ExperimentTable';
import type { Experiment } from '../lib/types';

function renderTable(experiment: Experiment) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <ExperimentTable experiments={[experiment]} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('选型研究列表结论', () => {
  it('优先显示 benchmark 生成的结果结论', () => {
    renderTable({
      id: 'exp-vgo',
      name: 'VGO 7-Zip 快速可行性测试',
      status: 'completed',
      mode: 'selection',
      benchmarkName: 'VGO 性能波动与稳定性测试',
      resultConclusion: '快速验证 · 7-Zip：优化未改善波动（CV 0.28%→0.72%）',
      attempts: 1,
      maxAttempts: 1,
      progress: 100,
      createdAt: '2026-08-26T07:08:13Z',
    });

    expect(screen.getByText('快速验证 · 7-Zip：优化未改善波动（CV 0.28%→0.72%）')).toBeInTheDocument();
    expect(screen.queryByText('待执行')).not.toBeInTheDocument();
  });

  it('运行中的研究显示测试中而不是待执行', () => {
    renderTable({
      id: 'exp-running',
      name: 'VGO LBM + SAD 快速可行性测试',
      status: 'running',
      mode: 'selection',
      benchmarkName: 'VGO 性能波动与稳定性测试',
      attempts: 0,
      maxAttempts: 2,
      progress: 0,
      createdAt: '2026-08-26T07:20:00Z',
    });

    expect(screen.getByText('测试中')).toBeInTheDocument();
    expect(screen.queryByText('待执行')).not.toBeInTheDocument();
  });
});
