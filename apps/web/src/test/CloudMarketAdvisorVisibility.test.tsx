import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CreateExperimentPage } from '../pages/CreateExperimentPage';

function response(data: unknown) {
  return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function renderCreate() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter><CreateExperimentPage /></MemoryRouter></QueryClientProvider>);
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
    expect(screen.getByRole('button', { name: '收起选型助手' })).toHaveClass('secondary', 'open');
    expect(screen.getByRole('button', { name: '腾讯云 CVM' })).toHaveClass('selected');
    expect(screen.getByRole('button', { name: /阿里云 ECS/ })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /阿里云 ECS/ }));
    await waitFor(() => expect(screen.getByRole('button', { name: /阿里云 ECS/ })).toHaveClass('selected'));
    expect(screen.getByRole('status')).toHaveTextContent('云厂商尚未连接');
  });
});
