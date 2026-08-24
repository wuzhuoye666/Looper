import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { AlibabaSelectionAdvisor } from '../components/AlibabaSelectionAdvisor';
import type { CloudInstanceType } from '../lib/types';

function response(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function advisorResponse(
  offset: number,
  provider: 'alibaba' | 'tencent' = 'alibaba',
  query = '',
  aggregate = false,
) {
  const id = provider === 'tencent'
    ? (offset === 0 ? 'M8.LARGE32' : 'M7.LARGE32')
    : (query.toLowerCase().includes('i8i') || offset > 0 ? 'ecs.i8i.xlarge' : 'ecs.i9i.xlarge');
  const region = provider === 'tencent' ? 'ap-test' : 'cn-test';
  const zone = provider === 'tencent' || aggregate ? undefined : 'cn-test-a';
  return {
    provider,
    region,
    zone,
    items: [{
      provider, region, id, family: provider === 'tencent' ? id.split('.')[0] : id.split('.').slice(0, 2).join('.'),
      typeLabel: provider === 'tencent' ? '内存型' : '本地存储型',
      familyLabel: provider === 'tencent' ? `内存型 ${id.split('.')[0]}` : `本地 SSD 型 ${id.split('.')[1]}`,
      cpu: 8, memoryGib: 32, gpu: 0, architecture: 'X86', zones: zone ? [zone] : ['ap-test-1'], available: true,
      localStorageCount: 1, localStorageCapacityGib: 1900, localStorageCategory: 'local_ssd_pro',
      attributes: provider === 'tencent' || aggregate ? { zoneCapabilities: [{ zone: provider === 'tencent' ? 'ap-test-1' : 'cn-test-a', available: true, localStorageCategory: 'LOCAL_SSD' }] } : {},
      matchTier: 'preferred', reasons: ['规格族优先匹配数据库场景', '精确匹配 8 vCPU / 32 GiB'],
      warnings: provider === 'tencent' || aggregate ? [`地域聚合结果，需选择可用区确认；当前匹配：${provider === 'tencent' ? 'ap-test-1' : 'cn-test-a'}`] : [],
    }],
    total: query ? 1 : 21,
    eligibleTotal: 21,
    offset,
    limit: 20,
    nextOffset: !query && offset === 0 ? 20 : null,
    exclusionStages: [
      { code: 'availability', label: '可用区库存', before: 30, after: 28, removed: 2 },
      { code: 'exact-spec', label: '精确 CPU / 内存', before: 28, after: 21, removed: 7 },
    ],
    mostRestrictiveStage: { code: 'exact-spec', label: '精确 CPU / 内存', before: 28, after: 21, removed: 7 },
    source: 'live', fetchedAt: '2026-08-22T00:00:00Z', expiresAt: '2026-08-22T00:05:00Z', stale: false,
  };
}

function Harness({ provider = 'alibaba' }: { provider?: 'alibaba' | 'tencent' }) {
  const [region, setRegion] = useState('');
  const [zone, setZone] = useState('');
  const [selected, setSelected] = useState<CloudInstanceType | null>(null);
  const regionId = provider === 'tencent' ? 'ap-test' : 'cn-test';
  const zoneId = provider === 'tencent' ? 'ap-test-1' : 'cn-test-a';
  return <>
    <AlibabaSelectionAdvisor
      provider={provider}
      regions={[{ provider, id: regionId, name: '测试地域', available: true }]}
      zones={[{ provider, region: regionId, id: zoneId, name: '测试一区', available: true }]}
      region={region}
      zone={zone}
      onRegionChange={value => { setRegion(value); setZone(''); setSelected(null); }}
      onZoneChange={value => { setZone(value); setSelected(null); }}
      selected={selected}
      onSelect={setSelected}
    />
    <output data-testid="selected-instance">{selected?.id || ''}</output>
  </>;
}

function renderAdvisor(provider: 'alibaba' | 'tencent' = 'alibaba') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><Harness provider={provider} /></QueryClientProvider>);
}

async function completeDatabaseQuestionnaire(
  region = 'cn-test',
  zone: string | null = 'cn-test-a',
) {
  fireEvent.click(screen.getByRole('button', { name: /数据库/ }));
  fireEvent.change(screen.getByLabelText('精确 vCPU'), { target: { value: '8' } });
  fireEvent.change(screen.getByLabelText('精确内存 GiB'), { target: { value: '32' } });
  fireEvent.click(screen.getByRole('button', { name: '必须' }));
  fireEvent.click(screen.getByRole('button', { name: /继续设置部署约束/ }));
  fireEvent.click(screen.getByRole('button', { name: /可以提供/ }));
  fireEvent.click(screen.getByRole('button', { name: 'x86' }));
  fireEvent.change(screen.getByLabelText('助手地域'), { target: { value: region } });
  if (zone) fireEvent.change(screen.getByLabelText('助手可用区'), { target: { value: zone } });
  fireEvent.click(screen.getByRole('button', { name: /查看推荐结果/ }));
}

describe('阿里云 ECS 选型助手', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('CPU 与内存直接作为可选输入，代码可用性保持单选', async () => {
    const requests: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      requests.push(body);
      return response(advisorResponse(0));
    }));
    renderAdvisor();
    fireEvent.click(screen.getByRole('button', { name: /数据库/ }));

    expect(screen.queryByRole('button', { name: '知道配置' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '暂不清楚' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('精确 vCPU')).toHaveAttribute('placeholder', '可选');
    expect(screen.getByLabelText('精确内存 GiB')).toHaveAttribute('placeholder', '可选');
    expect(screen.getByRole('button', { name: /继续设置部署约束/ })).toBeEnabled();

    fireEvent.click(screen.getByRole('button', { name: /继续设置部署约束/ }));
    const available = screen.getByRole('button', { name: '可以提供' });
    const unavailable = screen.getByRole('button', { name: '无法提供' });
    const unknown = screen.getByRole('button', { name: '暂不确定' });
    expect(unknown).toHaveClass('active');
    expect(available).not.toHaveClass('active');
    expect(unavailable).not.toHaveClass('active');

    fireEvent.click(unavailable);
    expect(unavailable).toHaveClass('active');
    expect(unknown).not.toHaveClass('active');
    expect(screen.getByText('候选结果会提示兼容性风险')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('助手地域'), { target: { value: 'cn-test' } });
    fireEvent.click(screen.getByRole('button', { name: /查看推荐结果/ }));
    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0]).toMatchObject({ sizingMode: 'unknown', codeAvailability: 'unavailable' });
    expect(requests[0].exactCpu).toBeUndefined();
    expect(requests[0].exactMemoryGib).toBeUndefined();
  });

  it('按前序答案展示分支、精确筛选并加载更多候选', async () => {
    const requests: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      requests.push(body);
      return response(advisorResponse(Number(body.offset || 0), 'alibaba', String(body.query || '')));
    }));
    const view = renderAdvisor();

    expect(screen.getByRole('heading', { name: '主要使用场景是什么？' })).toBeInTheDocument();
    expect(screen.getByLabelText('选型进度 1 / 4')).toBeInTheDocument();
    expect(screen.queryByLabelText(/选型进度 .*\/ 7/)).not.toBeInTheDocument();
    expect(screen.queryByText('完成 3 项设置后生成推荐')).not.toBeInTheDocument();
    expect(view.container.querySelector('.advisor-results')).not.toBeInTheDocument();
    expect(view.container.querySelector('.advisor-market-layout')).toHaveClass('questionnaire');
    expect(view.container.querySelector('input[type="file"]')).not.toBeInTheDocument();
    await completeDatabaseQuestionnaire();

    expect(await screen.findByRole('heading', { name: '剩余 21 个候选' })).toBeInTheDocument();
    expect(view.container.querySelector('.advisor-results')).toBeInTheDocument();
    expect(view.container.querySelector('.advisor-market-layout')).not.toHaveClass('questionnaire');
    expect(screen.getByText('ecs.i9i.xlarge')).toBeInTheDocument();
    expect(screen.getByText(/本地存储型 · 本地 SSD 型 i9i/)).toBeInTheDocument();
    expect(requests[0]).toMatchObject({
      primaryScenario: 'database', sizingMode: 'exact', exactCpu: 8, exactMemoryGib: 32,
      localStorage: 'required', codeAvailability: 'available', architecture: 'x86', offset: 0, limit: 20,
    });

    fireEvent.change(screen.getByLabelText('搜索候选机型'), { target: { value: 'ecs.i8i' } });
    expect(requests[requests.length - 1].query).toBeUndefined();
    expect(screen.getByText('内容尚未确认，当前结果保持不变')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '确认' }));
    expect(await screen.findByText('ecs.i8i.xlarge')).toBeInTheDocument();
    await waitFor(() => expect(requests[requests.length - 1]).toMatchObject({ query: 'ecs.i8i', offset: 0, limit: 20 }));
    expect(screen.getByRole('heading', { name: '匹配 1 个候选' })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('搜索候选机型'), { target: { value: '本地存储型' } });
    expect(requests[requests.length - 1]).toMatchObject({ query: 'ecs.i8i' });
    fireEvent.click(screen.getByRole('button', { name: '确认' }));
    await waitFor(() => expect(requests[requests.length - 1]).toMatchObject({ query: '本地存储型', offset: 0, limit: 20 }));
    await screen.findByText('ecs.i9i.xlarge');

    const search = screen.getByLabelText('搜索候选机型');
    for (const value of ['本地存储', '本地存', '本地', '本', '']) {
      fireEvent.change(search, { target: { value } });
    }
    expect(search).toHaveValue('');
    expect(requests[requests.length - 1]).toMatchObject({ query: '本地存储型' });
    fireEvent.click(screen.getByRole('button', { name: '确认' }));
    expect(await screen.findByText('ecs.i9i.xlarge')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /加载更多/ }));
    expect(await screen.findByText('ecs.i8i.xlarge')).toBeInTheDocument();
    expect(requests[requests.length - 1]).toMatchObject({ offset: 20, limit: 20 });
  });

  it('选择机型后，修改硬约束会清除已选结果并解释原因', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      return response(advisorResponse(Number(body.offset || 0), 'alibaba', String(body.query || '')));
    }));
    renderAdvisor();
    await completeDatabaseQuestionnaire();

    fireEvent.click(await screen.findByRole('button', { name: '选择此机型' }));
    expect(screen.getByTestId('selected-instance')).toHaveTextContent('ecs.i9i.xlarge');
    fireEvent.click(screen.getByRole('button', { name: '资源需求' }));
    fireEvent.change(screen.getByLabelText('精确 vCPU'), { target: { value: '16' } });

    await waitFor(() => expect(screen.getByTestId('selected-instance')).toHaveTextContent(''));
  });

  it('腾讯云可在不选可用区时生成地域候选并发送正确厂商', async () => {
    const requests: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      requests.push(body);
      return response(advisorResponse(Number(body.offset || 0), 'tencent', String(body.query || '')));
    }));
    renderAdvisor('tencent');

    expect(screen.getByLabelText('腾讯云 CVM 选型助手')).toBeInTheDocument();
    await completeDatabaseQuestionnaire('ap-test', null);

    expect(await screen.findByText('M8.LARGE32')).toBeInTheDocument();
    expect(screen.getByText(/地域聚合结果，需选择可用区确认/)).toBeInTheDocument();
    expect(requests[0]).toMatchObject({ provider: 'tencent', region: 'ap-test', offset: 0 });
    expect(requests[0].zone).toBeUndefined();
  });

  it('阿里云地域聚合候选在未选可用区时保持有效并可选择', async () => {
    const requests: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      requests.push(body);
      return response(advisorResponse(
        Number(body.offset || 0),
        'alibaba',
        String(body.query || ''),
        true,
      ));
    }));
    renderAdvisor('alibaba');

    await completeDatabaseQuestionnaire('cn-test', null);

    expect(await screen.findByText('ecs.i9i.xlarge')).toBeInTheDocument();
    expect(screen.getByText(/地域聚合结果，需选择可用区确认/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '选择此机型' }));
    expect(screen.getByTestId('selected-instance')).toHaveTextContent('ecs.i9i.xlarge');
    expect(requests[0]).toMatchObject({ provider: 'alibaba', region: 'cn-test', offset: 0 });
    expect(requests[0].zone).toBeUndefined();
  });
});
