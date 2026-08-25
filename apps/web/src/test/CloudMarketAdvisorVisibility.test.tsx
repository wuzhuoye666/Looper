import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CreateExperimentPage } from '../pages/CreateExperimentPage';

function response(data: unknown) {
  return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function renderCreate() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><CreateExperimentPage /><LocationProbe /></MemoryRouter></QueryClientProvider>);
}

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="location">{location.pathname}{location.search}</div>;
}

describe('选型研究页的云选型助手', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/benchmarks')) return response({ items: [] });
      if (url.endsWith('/cloud/providers')) return response({ items: [
        { id: 'tencent', name: '腾讯云 CVM', sdkPackage: 'cvm', sdkInstalled: true, credentialsConfigured: true, missingEnvironment: [], capabilities: [], livePurchaseEnabled: false },
        { id: 'alibaba', name: '阿里云 ECS', sdkPackage: 'ecs', sdkInstalled: false, credentialsConfigured: false, missingEnvironment: ['ALIBABA_CLOUD_ACCESS_KEY_ID'], capabilities: [], livePurchaseEnabled: false },
      ] });
      if (url.includes('/cloud/catalog/tencent/region')) return response({ items: [{ provider: 'tencent', id: 'ap-test', name: '测试地域', available: true }] });
      if (url.includes('/cloud/catalog/tencent/zone')) return response({ items: [{ provider: 'tencent', region: 'ap-test', id: 'ap-test-1', name: '测试一区', available: true }] });
      if (url.includes('/cloud/selection-advisor/search')) return response({
        provider: 'tencent', region: 'ap-test', zone: 'ap-test-1',
        items: [{
          provider: 'tencent', region: 'ap-test', id: 'S9.ADVISOR', family: 'S9', cpu: 4, memoryGib: 8,
          architecture: 'X86', zones: ['ap-test-1'], available: true, matchTier: 'preferred',
          reasons: ['规格族适合 Web / API 场景'], warnings: [],
        }],
        total: 1, eligibleTotal: 1, offset: 0, limit: 20, nextOffset: null,
        exclusionStages: [{ code: 'availability', label: '可用区库存', before: 1, after: 1, removed: 0 }],
        source: 'live', fetchedAt: '2026-08-24T00:00:00Z', expiresAt: '2026-08-24T00:05:00Z', stale: false,
        topPicks: [{
          category: 'balanced', label: '均衡型', reason: '测试推荐理由 S9.ADVISOR',
          scores: { scenarioRank: 0, performance: 48, estimatedHourly: 0.781, valuePerYuan: 61.5 },
          item: {
            provider: 'tencent', region: 'ap-test', id: 'S9.ADVISOR', family: 'S9', cpu: 4, memoryGib: 8,
            architecture: 'X86', zones: ['ap-test-1'], available: true, matchTier: 'preferred',
            reasons: ['规格族适合 Web / API 场景'], warnings: [],
          },
          price: { hourlyAmount: '0.781', currency: 'CNY', source: 'estimated' },
        }],
      });
      return response({ items: [] });
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('默认收起助手，展开后提供横向云厂商按钮并支持切换', async () => {
    renderCreate();
    expect(screen.getByRole('button', { name: '打开选型助手' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('button', { name: '打开选型助手' })).toHaveClass('primary');
    expect(screen.queryByRole('region', { name: '云服务器选型助手' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '打开选型助手' }));
    expect(await screen.findByRole('region', { name: '云服务器选型助手' })).toBeInTheDocument();
    expect(screen.queryByRole('list', { name: '创建步骤' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('研究名称 *')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '收起选型助手' })).toHaveClass('secondary', 'open');
    expect(screen.getByRole('button', { name: '腾讯云 CVM' })).toHaveClass('selected');
    expect(screen.getByRole('button', { name: /阿里云 ECS/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /阿里云 ECS/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: /阿里云 ECS/ })).toHaveClass('selected'));
    expect(screen.getByRole('status')).toHaveTextContent('云厂商尚未连接');

    fireEvent.click(screen.getByRole('button', { name: '收起选型助手' }));
    expect(screen.getByRole('list', { name: '创建步骤' })).toBeInTheDocument();
    expect(screen.getByLabelText('研究名称 *')).toBeInTheDocument();
  });

  it('选择推荐机型后先保存，用户确认后再携带完整购买配置跳转', async () => {
    renderCreate();
    fireEvent.click(screen.getByRole('button', { name: '打开选型助手' }));
    fireEvent.click(await screen.findByRole('button', { name: /Web \/ API/ }));
    fireEvent.click(screen.getByRole('button', { name: /继续设置部署约束/ }));
    await waitFor(() => expect(screen.getByLabelText('助手地域')).toHaveValue('ap-test'));
    await waitFor(() => expect(screen.getByLabelText('助手可用区')).toHaveValue('ap-test-1'));
    fireEvent.click(screen.getByRole('button', { name: /查看推荐结果/ }));
    fireEvent.click(await screen.findByRole('button', { name: '选择此机型' }));

    expect(screen.getByTestId('location')).toHaveTextContent('/');
    expect(screen.getByText(/S9\.ADVISOR · 4 vCPU \/ 8 GiB/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /阿里云 ECS/ }));
    expect(screen.queryByText(/S9\.ADVISOR · 4 vCPU \/ 8 GiB/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '腾讯云 CVM' }));
    expect(screen.getByText(/S9\.ADVISOR · 4 vCPU \/ 8 GiB/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /打开购买配置/ }));
    expect(screen.getByTestId('location')).toHaveTextContent(
      '/cloud/market?provider=tencent&region=ap-test&instanceType=S9.ADVISOR&zone=ap-test-1',
    );
  });
});
