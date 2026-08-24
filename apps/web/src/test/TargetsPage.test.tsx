import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TargetsPage } from '../pages/CatalogPages';

function response(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderTargets() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <TargetsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('候选资源云库存', () => {
  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/targets')) return response({ items: [
        { id: 'cloud:tencent:ap-guangzhou:ins-gz', name: '腾讯广州机器', type: 'tencent', provider: 'tencent', status: 'inventory', lifecycleStatus: 'active', fingerprint: { region: 'ap-guangzhou' } },
        { id: 'cloud:tencent:ap-shanghai:ins-sh', name: '腾讯上海机器', type: 'tencent', provider: 'tencent', status: 'inventory', lifecycleStatus: 'active', fingerprint: { region: 'ap-shanghai' } },
        { id: 'cloud:alibaba:cn-guangzhou:i-gz', name: '阿里广州机器', type: 'alibaba', provider: 'alibaba', status: 'inventory', lifecycleStatus: 'active', fingerprint: { region: 'cn-guangzhou' } },
        { id: 'cloud:alibaba:cn-hangzhou:i-hz', name: '阿里杭州机器', type: 'alibaba', provider: 'alibaba', status: 'inventory', lifecycleStatus: 'active', fingerprint: { region: 'cn-hangzhou' } },
        { id: 'external:10.0.0.8', name: '外部机器', type: 'external', provider: 'external', status: 'online', lifecycleStatus: 'active', fingerprint: {} },
      ] });
      return response({ items: [], total: 0 });
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('展示腾讯云和阿里云所有地域的机器并保留外部机器', async () => {
    renderTargets();

    expect(await screen.findByText('腾讯广州机器')).toBeInTheDocument();
    expect(screen.getByText('阿里广州机器')).toBeInTheDocument();
    expect(screen.getByText('外部机器')).toBeInTheDocument();
    expect(screen.getByText('腾讯上海机器')).toBeInTheDocument();
    expect(screen.getByText('阿里杭州机器')).toBeInTheDocument();
    expect(screen.getByText('5 个资源')).toBeInTheDocument();
  });

  it('通过一个按钮调用全地域云库存同步', async () => {
    renderTargets();
    const sync = await screen.findByRole('button', { name: '同步云库存' });

    expect(screen.queryByRole('button', { name: '同步腾讯云库存' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '同步阿里云库存' })).not.toBeInTheDocument();
    fireEvent.click(sync);

    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls.map(([input]) => String(input));
      expect(calls.some(url => url.endsWith('/targets/cloud/sync'))).toBe(true);
      expect(calls.some(url => url.includes('/targets/tencent-cvm/sync'))).toBe(false);
      expect(calls.some(url => url.includes('/targets/alibaba-ecs/sync'))).toBe(false);
    });
  });
});
