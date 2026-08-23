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

function advisorResponse(offset: number) {
  const id = offset === 0 ? 'ecs.i9i.xlarge' : 'ecs.i8i.xlarge';
  return {
    provider: 'alibaba',
    region: 'cn-test',
    zone: 'cn-test-a',
    items: [{
      provider: 'alibaba', region: 'cn-test', id, family: id.split('.').slice(0, 2).join('.'),
      cpu: 8, memoryGib: 32, gpu: 0, architecture: 'X86', zones: ['cn-test-a'], available: true,
      localStorageCount: 1, localStorageCapacityGib: 1900, localStorageCategory: 'local_ssd_pro',
      matchTier: 'preferred', reasons: ['规格族优先匹配数据库场景', '精确匹配 8 vCPU / 32 GiB'], warnings: [],
    }],
    total: 21,
    offset,
    limit: 20,
    nextOffset: offset === 0 ? 20 : null,
    exclusionStages: [
      { code: 'availability', label: '可用区库存', before: 30, after: 28, removed: 2 },
      { code: 'exact-spec', label: '精确 CPU / 内存', before: 28, after: 21, removed: 7 },
    ],
    mostRestrictiveStage: { code: 'exact-spec', label: '精确 CPU / 内存', before: 28, after: 21, removed: 7 },
    source: 'live', fetchedAt: '2026-08-22T00:00:00Z', expiresAt: '2026-08-22T00:05:00Z', stale: false,
  };
}

function Harness() {
  const [region, setRegion] = useState('');
  const [zone, setZone] = useState('');
  const [selected, setSelected] = useState<CloudInstanceType | null>(null);
  return <>
    <AlibabaSelectionAdvisor
      regions={[{ provider: 'alibaba', id: 'cn-test', name: '测试地域', available: true }]}
      zones={[{ provider: 'alibaba', region: 'cn-test', id: 'cn-test-a', name: '测试一区', available: true }]}
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

function renderAdvisor() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>);
}

async function completeDatabaseQuestionnaire() {
  fireEvent.click(screen.getByRole('button', { name: /数据库/ }));
  fireEvent.click(screen.getByRole('button', { name: /下一题/ }));
  fireEvent.click(screen.getByRole('button', { name: '知道配置' }));
  fireEvent.change(screen.getByLabelText('精确 vCPU'), { target: { value: '8' } });
  fireEvent.change(screen.getByLabelText('精确内存 GiB'), { target: { value: '32' } });
  fireEvent.click(screen.getByRole('button', { name: /下一题/ }));
  fireEvent.click(screen.getByRole('button', { name: '必须' }));
  fireEvent.click(screen.getByRole('button', { name: /下一题/ }));
  fireEvent.click(screen.getByRole('button', { name: /可以提供/ }));
  fireEvent.click(screen.getByRole('button', { name: 'x86' }));
  fireEvent.change(screen.getByLabelText('助手地域'), { target: { value: 'cn-test' } });
  fireEvent.change(screen.getByLabelText('助手可用区'), { target: { value: 'cn-test-a' } });
  fireEvent.click(screen.getByRole('button', { name: /查看候选/ }));
}

describe('阿里云 ECS 选型助手', () => {
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('按前序答案展示分支、精确筛选并加载更多候选', async () => {
    const requests: Array<Record<string, unknown>> = [];
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      requests.push(body);
      return response(advisorResponse(Number(body.offset || 0)));
    }));
    const view = renderAdvisor();

    expect(screen.getByRole('heading', { name: '主要使用场景是什么？' })).toBeInTheDocument();
    expect(view.container.querySelector('input[type="file"]')).not.toBeInTheDocument();
    await completeDatabaseQuestionnaire();

    expect(await screen.findByRole('heading', { name: '剩余 21 个候选' })).toBeInTheDocument();
    expect(screen.getByText('ecs.i9i.xlarge')).toBeInTheDocument();
    expect(requests[0]).toMatchObject({
      primaryScenario: 'database', sizingMode: 'exact', exactCpu: 8, exactMemoryGib: 32,
      localStorage: 'required', codeAvailability: 'available', architecture: 'x86', offset: 0, limit: 20,
    });

    fireEvent.click(screen.getByRole('button', { name: /加载更多/ }));
    expect(await screen.findByText('ecs.i8i.xlarge')).toBeInTheDocument();
    expect(requests[1]).toMatchObject({ offset: 20, limit: 20 });
  });

  it('选择机型后，修改硬约束会清除已选结果并解释原因', async () => {
    vi.stubGlobal('fetch', vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      const body = JSON.parse(String(init?.body || '{}')) as Record<string, unknown>;
      return response(advisorResponse(Number(body.offset || 0)));
    }));
    renderAdvisor();
    await completeDatabaseQuestionnaire();

    fireEvent.click(await screen.findByRole('button', { name: '选择此机型' }));
    expect(screen.getByTestId('selected-instance')).toHaveTextContent('ecs.i9i.xlarge');
    fireEvent.click(screen.getByRole('button', { name: /规格/ }));
    fireEvent.change(screen.getByLabelText('精确 vCPU'), { target: { value: '16' } });

    await waitFor(() => expect(screen.getByTestId('selected-instance')).toHaveTextContent(''));
  });
});
