import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';

const expiredOrder = {
  id: 'order-expired',
  quoteId: 'quote-expired',
  provider: 'tencent',
  status: 'expired',
  spec: {
    provider: 'tencent', region: 'ap-test', zone: 'ap-test-1', instanceType: 'S5.SMALL1',
    imageId: 'img-test', instanceName: 'renew-me', count: 1, billingMode: 'postpaid',
    vpcId: 'vpc-test', subnetId: 'subnet-test', securityGroupIds: ['sg-test'],
    systemDiskGib: 50, publicIp: false, internetBandwidthMbps: 0, tags: {},
  },
  specDigest: 'sha256:spec', quoteDigest: 'sha256:quote', hourlyAmount: '0.12', currency: 'CNY',
  instanceIds: [], providerResponse: {}, confirmationExpiresAt: '2020-01-01T00:00:00Z',
  createdAt: '2026-08-21T00:00:00Z', updatedAt: '2026-08-21T00:05:00Z',
};

function response(data: unknown) {
  return new Response(JSON.stringify(data), { status: 200, headers: { 'Content-Type': 'application/json' } });
}

function renderOrder() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/cloud/orders/order-expired']} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe('订单详情不再提供二次确认', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.sessionStorage.setItem('looper.operator-token', 'operator-token-renewal-test-123456789');
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/operator/session')) return response({ required: true, configured: true, authenticated: true, operatorGateReady: true });
      if (url.endsWith('/cloud/orders/order-expired/events')) return response({ items: [], total: 0 });
      if (url.endsWith('/cloud/orders/order-expired')) return response(expiredOrder);
      return response({ items: [] });
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('过期订单只展示结果，不展示确认令牌或续签入口', async () => {
    renderOrder();
    expect(await screen.findByText('已过期')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '二次确认' })).not.toBeInTheDocument();
    expect(screen.queryByText('确认令牌')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /重新验证并继续/ })).not.toBeInTheDocument();
  });
});
