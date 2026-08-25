import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { App } from '../App';

const draft = {
  build: {
    dockerfile: 'FROM python:3.12-slim\nCOPY . /app', compose: 'services:\n  app:\n    build: .',
    startCommand: 'python app.py', healthPath: '/health', servicePort: 8000,
    dependencies: [], unresolved: [], evidence: [{ file: 'app.py', startLine: 1, endLine: 5 }], approved: false,
  },
  scenario: {
    steps: [{ id: 'step-1', interfaceId: 'create-order', label: '创建订单', method: 'POST', path: '/orders', headers: {}, body: {}, extract: {}, assertions: [{ kind: 'status', field: '', expected: 201 }], sideEffect: 'write' }],
    resetStrategy: 'none', resetCommand: '',
  },
  slo: { minimumSuccessRate: .999, maximumErrorRate: .001, maximumTimeoutRate: .001, p99Ms: 500, p999Ms: 1000, confidenceLevel: .95, minimumSamples: 1000 },
  targets: { sutIds: [], internalLoadGeneratorId: '', externalLoadGeneratorId: '', internalBaseUrls: {}, externalBaseUrls: {} },
  budget: { maxSeconds: 3600, maxAttempts: 80, costCap: 10, referenceRps: 100, measurementSeconds: 20 },
};

function study(overrides: Record<string, unknown> = {}) {
  return {
    id: 'capacity_test', discoveryId: 'discovery_test', discoveryName: 'orders.zip', sourceDigest: 'sha256:source',
    sourceArchive: { status: 'retained', expiresAt: '2099-08-24T00:00:00Z', encryptedAtRest: true, keyProtection: 'owner-key-file' },
    name: '订单容量测试', status: 'draft', revision: 1, currentStep: 0,
    draft: structuredClone(draft), constraints: [], readyToPreflight: false, preflight: {},
    execution: { phases: [], runs: [] }, report: null, error: null,
    createdAt: '2026-08-24T00:00:00Z', updatedAt: '2026-08-24T00:00:00Z',
    ...overrides,
  };
}

function renderRoute(route: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><App /></MemoryRouter></QueryClientProvider>);
}

let current: any;

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

beforeEach(() => {
  window.sessionStorage.clear();
  current = study();
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/capacity-studies/capacity_test') && init?.method === 'PATCH') {
      const body = JSON.parse(String(init.body));
      current = { ...current, revision: current.revision + 1, currentStep: body.currentStep, draft: body.draft, preflight: {} };
      return Response.json(current);
    }
    if (url.endsWith('/capacity-studies/capacity_test/build/repair') && init?.method === 'POST') {
      current = { ...current, revision: current.revision + 1, draft: { ...current.draft, build: { ...current.draft.build, sourceRoot: 'poll-service-master', unresolved: [], advisories: ['健康检查不能验证数据库就绪'], checks: [{ id: 'source-root', label: '源码根目录', status: 'fixed', detail: 'ZIP 顶层目录已识别为 poll-service-master' }], approved: false } }, preflight: {} };
      return Response.json(current);
    }
    if (url.endsWith('/capacity-studies/capacity_test/preflight') && init?.method === 'POST') {
      current = { ...current, preflight: { status: 'pass', draftRevision: current.revision, checkedAt: '2026-08-24T01:00:00Z', failedSutIds: [], generatorFailures: [], checks: [{ scope: 'sut', targetId: 'sut-1', passed: true, detail: 'Docker Compose ready' }, { scope: 'load-generator', network: 'internal', targetId: 'loadgen-1', passed: true, detail: 'Worker 在线' }, { scope: 'load-generator', network: 'external', targetId: 'loadgen-1', passed: true, detail: 'Worker 在线' }] } };
      return Response.json(current);
    }
    if (url.endsWith('/capacity-studies/capacity_test/start') && init?.method === 'POST') {
      current = { ...current, status: 'queued', execution: { phases: [{ id: 'queued', status: 'running', at: '2026-08-24T01:01:00Z' }], selectedTargetIds: ['sut-1'], activeTargetIds: ['sut-1'], excludedTargetIds: [], runs: [] } };
      return Response.json(current);
    }
    if (url.endsWith('/capacity-studies/capacity_test')) return Response.json(current);
    if (url.endsWith('/capacity-studies')) return Response.json({ items: [current], total: 1 });
    if (url.endsWith('/targets')) return Response.json({ items: [
      { id: 'sut-1', name: '被测一号', status: 'online', lifecycleStatus: 'active', runnable: true, hardware: '4 vCPU' },
      { id: 'loadgen-1', name: '施压一号', status: 'online', lifecycleStatus: 'active', runnable: true, hardware: '8 vCPU' },
    ] });
    if (url.endsWith('/operator/session')) return Response.json({ required: true, configured: true, authenticated: false, operatorGateReady: true });
    return Response.json({});
  }));
});

it('五步向导保存业务链、服务器矩阵并通过真实预检启动', async () => {
  renderRoute('/capacity/capacity_test');
  expect(await screen.findByRole('heading', { name: '订单容量测试' })).toBeInTheDocument();
  expect(screen.getByRole('navigation', { name: '主导航' })).not.toHaveTextContent('容量测试');
  expect(screen.getByRole('button', { name: '选型' })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('button', { name: '可提供代码' })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('link', { name: '容量测试' })).toHaveClass('active');

  fireEvent.click(screen.getByRole('button', { name: /确认构建方案/ }));
  fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
  expect(await screen.findByRole('heading', { name: '编排一次完整业务迭代' })).toBeInTheDocument();
  fireEvent.change(screen.getByDisplayValue('无需重置'), { target: { value: 'compose-recreate' } });
  fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
  expect(screen.getByRole('heading', { name: '声明容量成立的 SLO' })).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
  expect(screen.getByRole('heading', { name: '选择被测服务器和施压机' })).toBeInTheDocument();

  fireEvent.click(screen.getByRole('checkbox', { name: /被测一号/ }));
  fireEvent.change(screen.getByLabelText('内网施压机'), { target: { value: 'loadgen-1' } });
  fireEvent.change(screen.getByLabelText('公网施压机'), { target: { value: 'loadgen-1' } });
  fireEvent.change(screen.getByLabelText('被测一号 内网 URL'), { target: { value: 'http://10.0.0.1:8000' } });
  fireEvent.change(screen.getByLabelText('被测一号 公网 URL'), { target: { value: 'http://203.0.113.1:8000' } });
  fireEvent.click(screen.getByRole('button', { name: /下一步/ }));
  expect(await screen.findByRole('heading', { name: '预算、执行矩阵与启动确认' })).toBeInTheDocument();

  await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([url, init]) => String(url).endsWith('/capacity-studies/capacity_test') && init?.method === 'PATCH')).toBe(true));
  const saves = vi.mocked(fetch).mock.calls.filter(([url, init]) => String(url).endsWith('/capacity-studies/capacity_test') && init?.method === 'PATCH');
  const saved = JSON.parse(String(saves[saves.length - 1]?.[1]?.body));
  expect(saved.currentStep).toBe(4);
  expect(saved.draft.scenario.resetStrategy).toBe('compose-recreate');
  expect(saved.draft.targets.sutIds).toEqual(['sut-1']);

  fireEvent.click(screen.getByRole('button', { name: '运行真实环境预检' }));
  expect(await screen.findByText('Docker Compose ready')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '开始容量测试' }));
  expect(await screen.findByText('运行中')).toBeInTheDocument();
  const start = vi.mocked(fetch).mock.calls.find(([url, init]) => String(url).endsWith('/capacity-studies/capacity_test/start') && init?.method === 'POST');
  expect(JSON.parse(String(start?.[1]?.body))).toMatchObject({ excludedTargetIds: [], acknowledgePartial: false });
});

it('普通用户可运行脚本诊断构建阻断项，无需编辑 Dockerfile', async () => {
  current = study({
    draft: { ...structuredClone(draft), build: { ...structuredClone(draft.build), unresolved: ['健康检查不能验证数据库就绪'] } },
  });
  renderRoute('/capacity/capacity_test');

  expect(await screen.findByText('当前有 1 个待验证问题')).toBeInTheDocument();
  expect(screen.getAllByText('健康检查不能验证数据库就绪').length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole('button', { name: '运行脚本诊断并修复' }));

  expect(await screen.findByText('静态脚本检查通过')).toBeInTheDocument();
  expect(screen.getByText('源码根目录 · 已自动修正')).toBeInTheDocument();
  expect(screen.getByText(/poll-service-master/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: /确认构建方案/ })).toBeEnabled();
  const repair = vi.mocked(fetch).mock.calls.find(([url, init]) => String(url).endsWith('/capacity-studies/capacity_test/build/repair') && init?.method === 'POST');
  expect(JSON.parse(String(repair?.[1]?.body))).toEqual({ expectedRevision: 1 });
});

it('完成后默认展示领导区间，并可切换工程证据与清理审计', async () => {
  const completed = study({
    status: 'completed', currentStep: 4,
    execution: { phases: [{ id: 'cleanup', status: 'completed', detail: '清理完成', at: '2026-08-24T02:00:00Z' }], selectedTargetIds: ['sut-1'], activeTargetIds: ['sut-1'], excludedTargetIds: [], cleanup: [{ targetId: 'sut-1', status: 'clean', cleanedAt: '2026-08-24T02:00:00Z' }] },
    report: { generatedAt: '2026-08-24T02:00:00Z', capacityUnit: 'successful-business-iterations/second', confidenceLevel: .95, decision: '证据支持容量区间。', networks: [{ network: 'internal', experimentId: 'exp-internal', status: 'resolved', targets: [{ target_id: 'sut-1', label: '被测一号', status: 'resolved', attempt_count: 15, valid_block_count: 15, invalid_block_count: 0, frontiers: { slo: { status: 'resolved', confirmed_pass: 100, confirmed_fail: 120 } }, metrics: [] }], comparisons: [], trajectory: [{ sequence: 1, offered_load: 100, origin: 'initial', status: 'pass', required_repeats: 5, reason: 'SLO pass' }], evidence: { attemptCount: 15 } }] },
  });
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith('/capacity-studies/capacity_test')) return Response.json(completed);
    if (url.endsWith('/targets')) return Response.json({ items: [] });
    return Response.json({});
  }));
  renderRoute('/capacity/capacity_test');
  expect(await screen.findByText('最高确认通过负载 ～ 最低确认失败负载')).toBeInTheDocument();
  expect(screen.getByText('100')).toBeInTheDocument();
  expect(screen.getByText('120')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '工程证据' }));
  expect(await screen.findByRole('link', { name: '打开原始证据' })).toHaveAttribute('href', '/experiments/exp-internal');
  fireEvent.click(screen.getByRole('button', { name: '审计' }));
  expect(await screen.findByRole('heading', { name: '清理证明' })).toBeInTheDocument();
  expect(screen.getAllByText('sut-1').length).toBeGreaterThan(0);
});
