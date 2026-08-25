import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';

const englishQuestion = 'How many successful full-stack MediaWiki requests can each concrete target SKU complete when every workload component shares one VM?';
const chineseQuestion = '当 MediaWiki 全部组件共享一台云服务器时，各服务器规格每秒能完成多少个成功请求？';

describe('选型研究概览', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    window.sessionStorage.clear();
    window.localStorage.clear();
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const data = url.endsWith('/experiments/exp-mediawiki') ? {
        id: 'exp-mediawiki',
        name: '测试3',
        status: 'completed',
        mode: 'selection',
        benchmarkId: 'dcperf.mediawiki.closed-loop',
        benchmarkName: 'DCPerf MediaWiki Closed-Loop',
        decisionQuestion: englishQuestion,
        targetIds: ['target-1'],
        targetNames: ['测试机3'],
        objective: 'closed_loop_successful_rps',
        progress: 100,
        attempts: 3,
        maxAttempts: 3,
        evaluations: [],
        scenario: {
          id: 'mediawiki-closed-loop', name: 'MediaWiki', decision_question: englishQuestion,
          user_value: 'web server selection', workload_class: 'web-full-stack',
          topology: 'closed-loop', primary_metric: 'closed_loop_successful_rps',
        },
        updatedAt: '2026-08-25T02:51:00Z',
      } : {};
      return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));
  });

  it('汉化顶部描述并移除重复的采购问题卡片', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/experiments/exp-mediawiki']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>);

    expect(await screen.findByText(chineseQuestion)).toBeInTheDocument();
    expect(screen.queryByText(englishQuestion)).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '采购问题' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '场景边界' })).toBeInTheDocument();
  });
});
