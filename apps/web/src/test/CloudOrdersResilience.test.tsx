import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';

const resilientOrder = {
  id: 'order-resilient',
  quoteId: 'quote-resilient',
  provider: 'tencent',
  status: 'succeeded',
  spec: {
    provider: 'tencent',
    region: 'ap-test',
    zone: 'ap-test-1',
    instanceType: 'S5.SMALL1',
    imageId: 'img-test',
    instanceName: 'resilient-instance',
    count: 1,
    billingMode: 'postpaid',
    vpcId: 'vpc-test',
    subnetId: 'subnet-test',
    securityGroupIds: ['sg-test'],
    systemDiskGib: 50,
    publicIp: false,
    internetBandwidthMbps: 0,
    tags: {},
  },
  specDigest: 'sha256:spec',
  quoteDigest: 'sha256:quote',
  hourlyAmount: '0.12',
  currency: 'CNY',
  instanceIds: [],
  providerResponse: {},
  confirmationExpiresAt: '2099-01-01T00:00:00Z',
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:05:00Z',
};

function response(data: unknown) {
  return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function renderOrders() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/cloud/orders']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('云订单加载恢复', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.sessionStorage.setItem('looper.operator-token', 'operator-token-cloud-order-retry-123456789');
    let orderRequests = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/operator/session')) return response({ required: true, configured: true, authenticated: true, operatorGateReady: true });
      if (url.endsWith('/cloud/orders')) {
        orderRequests += 1;
        if (orderRequests === 1) return new Response('temporary backend failure', { status: 503 });
        return response({ items: [resilientOrder], total: 1 });
      }
      return response({ items: [] });
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('临时服务端错误会自动恢复并展示订单', async () => {
    renderOrders();
    expect(await screen.findByRole('link', { name: /order-resilient/ })).toBeInTheDocument();
    expect(screen.queryByText('无法获取数据')).not.toBeInTheDocument();
  });
});
