import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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
          selectionReady: true, singleNodeReady: true, runnable: true, packageReady: true, tags: ['python'],
          selectionDefaults: { repeats: 1, timeout: 1200, seed: 101 },
          scenario: {
            id: 'registered.adapter', name: 'Registered Adapter',
            decision_question: 'Which target performs better?', user_value: 'Target selection',
            workload_class: 'database', topology: 'single-node', roles: [],
            primary_metric: 'score', slo_gates: [],
          },
        },
        {
          id: 'alternate.adapter', key: 'alternate.adapter@2.0.0',
          name: 'Alternate Adapter', version: '2.0.0', category: 'database',
          selectionReady: true, singleNodeReady: true, runnable: true, packageReady: true, tags: ['python'],
          selectionDefaults: { repeats: 7, timeout: 2400, seed: 202 },
          scenario: {
            id: 'alternate.adapter', name: 'Alternate Adapter',
            decision_question: 'Which alternate target performs better?', user_value: 'Target selection',
            workload_class: 'database', topology: 'single-node', roles: [],
            primary_metric: 'score', slo_gates: [],
          },
        },
        {
          id: 'dcperf.mediawiki.closed-loop', key: 'dcperf.mediawiki.closed-loop@1.0.0',
          name: 'DCPerf MediaWiki Closed-Loop', version: '1.0.0', category: 'web',
          selectionReady: true, singleNodeReady: true, runnable: true, packageReady: true,
          scenario: {
            id: 'dcperf', name: 'DCPerf', decision_question: 'Which server handles more requests?',
            user_value: 'Web capacity', workload_class: 'web-full-stack', topology: 'single-node',
            roles: [], primary_metric: 'closed_loop_successful_rps', slo_gates: [],
          },
        },
        {
          id: 'looper.vgo.variability', key: 'looper.vgo.variability@1.1.3',
          name: 'VGO 性能波动与稳定性测试', version: '1.1.3', category: 'performance-stability',
          selectionReady: true, singleNodeReady: true, runnable: true, packageReady: true,
          selectionDefaults: { repeats: 1, timeout: 86400, seed: 20260825 },
          scenario: {
            id: 'stability.vgo.cpu-variability', name: 'VGO',
            decision_question: 'Which target is more stable?', user_value: 'Stability',
            workload_class: 'cpu-performance-variability', topology: 'single-node', roles: [],
            primary_metric: 'runtime_cv', slo_gates: [],
          },
        },
        {
          id: 'incomplete.adapter', key: 'incomplete.adapter@1.0.0',
          name: 'Incomplete Adapter', version: '1.0.0', category: 'unclassified',
          selectionReady: false, runnable: false,
        },
        {
          id: 'looper.demo.compression', key: 'looper.demo.compression@1.1.0',
          name: 'Deterministic compression loop', version: '1.1.0', category: 'development-test',
          selectionReady: true, singleNodeReady: true, runnable: true, packageReady: true,
        },
      ] } : url.includes('/target-options') ? {
        benchmarkId: url.includes('alternate.adapter') ? 'alternate.adapter' : 'registered.adapter',
        version: url.includes('alternate.adapter') ? '2.0.0' : '1.0.0',
        topology: 'single-node', machineCount: 1,
        nodeGroup: { id: 'target', role: 'target', requirements: {}, summary: { osFamilies: ['linux'], architectures: [], capabilities: ['python'] } },
        environments: [{ id: 'external-ssh', label: '外部 SSH', compatibleCount: 1, targets: [{ id: 'target-a', name: 'Target A', provider: 'external', status: 'online', runnable: true, hardware: '8 vCPU / 32 GiB', tags: ['python'] }] }],
        rejectedSummary: [],
      } : { items: [] };
      return new Response(JSON.stringify(data), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      });
    }));
  });

  it('只显示具备完整部署包并可直接测试的 Benchmark', async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText('研究名称 *'), { target: { value: '新套件选型' } });
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));

    expect(await screen.findByRole('option', { name: '数据库：Registered Adapter' })).toBeEnabled();
    expect(screen.getByRole('option', { name: '网站承载能力（DCPerf）' })).toBeEnabled();
    expect(screen.queryByRole('option', { name: /Incomplete Adapter/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: /compression/i })).not.toBeInTheDocument();
    expect(screen.getAllByRole('option').every(option => (option.textContent?.length || 0) <= 22)).toBe(true);
    expect(screen.queryByRole('option', { name: '选择场景' })).not.toBeInTheDocument();
  });

  it('切换 Benchmark 时同步更新证据协议默认值', async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText('研究名称 *'), { target: { value: '默认值切换' } });
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));

    const scenario = await screen.findByLabelText(/想模拟的业务场景/);
    await waitFor(() => expect(scenario).toHaveValue('registered.adapter@1.0.0'));
    fireEvent.change(scenario, { target: { value: 'alternate.adapter@2.0.0' } });
    await waitFor(() => expect(scenario).toHaveValue('alternate.adapter@2.0.0'));
    fireEvent.change(await screen.findByLabelText('测试环境'), { target: { value: 'external-ssh' } });
    const target = await screen.findByRole('radio');
    fireEvent.click(target);
    const next = screen.getByRole('button', { name: /下一步/ });
    expect(next).toBeEnabled();
    fireEvent.click(next);

    expect(await screen.findByLabelText('每个目标重复数')).toHaveValue(7);
    expect(screen.getByLabelText('最长测试时间（秒）')).toHaveValue(2400);
    expect(screen.getByLabelText('测试顺序随机种子')).toHaveValue(202);
  });

  it('允许套件声明单次外层重复以承载内部多轮诊断', async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText('研究名称 *'), { target: { value: '最小完整诊断' } });
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));

    await waitFor(() => expect(screen.getByLabelText(/想模拟的业务场景/)).toHaveValue('registered.adapter@1.0.0'));
    fireEvent.change(await screen.findByLabelText('测试环境'), { target: { value: 'external-ssh' } });
    fireEvent.click(await screen.findByRole('radio'));
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));

    const repeats = await screen.findByLabelText('每个目标重复数');
    expect(repeats).toHaveValue(1);
    expect(repeats).toHaveAttribute('min', '1');
  });

  it('VGO 快速方案可仅为当次研究选择 LBM 和 SAD', async () => {
    renderPage();
    fireEvent.change(screen.getByLabelText('研究名称 *'), { target: { value: 'VGO 快速验证' } });
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));

    const scenario = await screen.findByLabelText(/想模拟的业务场景/);
    await waitFor(() => expect(scenario).toHaveValue('registered.adapter@1.0.0'));
    fireEvent.change(scenario, { target: { value: 'looper.vgo.variability@1.1.3' } });
    await waitFor(() => {
      expect(scenario).toHaveValue('looper.vgo.variability@1.1.3');
      expect(screen.getAllByText(/哪种服务器在长时运行中的性能波动更小/).length).toBeGreaterThan(0);
    });
    fireEvent.change(await screen.findByLabelText('测试环境'), { target: { value: 'external-ssh' } });
    fireEvent.click(await screen.findByRole('radio'));
    fireEvent.click(screen.getByRole('button', { name: /下一步/ }));

    const quick = await screen.findByRole('checkbox', { name: /仅本次快速可行性测试/ });
    expect(quick).not.toBeChecked();
    fireEvent.click(quick);
    fireEvent.click(screen.getByRole('checkbox', { name: '7-Zip' }));
    fireEvent.click(screen.getByRole('checkbox', { name: 'LBM' }));
    fireEvent.click(screen.getByRole('checkbox', { name: /SAD/ }));
    fireEvent.click(screen.getByRole('button', { name: '保存选型研究' }));

    await waitFor(() => {
      const createCall = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === 'POST');
      expect(createCall).toBeDefined();
      const payload = JSON.parse(String(createCall?.[1]?.body));
      expect(payload.workloadIds).toEqual(['lbm', 'sad']);
      expect(payload.selectionParameters).toEqual({
        diagnostic_scale_percent: 1,
        ab_blocks: 2,
        warmups: 0,
      });
    });
  });
});
