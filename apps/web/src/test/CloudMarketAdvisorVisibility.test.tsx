import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';

function response(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderMarket() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/cloud/market']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('阿里云选型助手入口', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/cloud/providers')) return response({ items: [
        {
          id: 'tencent', name: '腾讯云 CVM', sdkPackage: 'tencentcloud-sdk-python-cvm', sdkInstalled: true,
          credentialsConfigured: false, missingEnvironment: ['TENCENTCLOUD_SECRET_ID'], capabilities: [], livePurchaseEnabled: false,
        },
        {
          id: 'alibaba', name: '阿里云 ECS', sdkPackage: 'alibabacloud_ecs20140526', sdkInstalled: true,
          credentialsConfigured: false, missingEnvironment: ['ALIBABA_CLOUD_ACCESS_KEY_ID'], capabilities: [], livePurchaseEnabled: false,
        },
      ] });
      if (url.endsWith('/cloud/purchase-readiness')) return response({
        livePurchaseEnabled: false,
        operatorTokenReady: false,
        confirmationSecretReady: false,
        maxHourlyAmount: '10',
        providers: [
          { provider: 'tencent', name: '腾讯云 CVM', ready: false, missingEnvironment: ['TENCENTCLOUD_SECRET_ID'], checks: [] },
          { provider: 'alibaba', name: '阿里云 ECS', ready: false, missingEnvironment: ['ALIBABA_CLOUD_ACCESS_KEY_ID'], checks: [] },
        ],
      });
      if (url.endsWith('/operator/session')) return response({ required: false, configured: false, authenticated: true, operatorGateReady: true });
      return response({ items: [] });
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('未配置阿里云凭据时仍展示需求问卷', async () => {
    renderMarket();
    fireEvent.click(await screen.findByRole('button', { name: /阿里云 ECS.*等待凭证/ }));

    const advisor = await screen.findByRole('region', { name: '阿里云 ECS 选型助手' });
    expect(within(advisor).getByRole('heading', { name: '选型助手' })).toBeInTheDocument();
    expect(within(advisor).getByRole('heading', { name: '主要使用场景是什么？' })).toBeInTheDocument();
    expect(screen.getByText('阿里云 ECS 尚未连接')).toBeInTheDocument();
  });
});
