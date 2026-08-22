import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
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

describe('过期订单确认续期', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.sessionStorage.setItem('looper.operator-token', 'operator-token-renewal-test-123456789');
    let renewed = false;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/cloud/auth/status')) return response({ required: true, configured: true, authenticated: true, operatorGateReady: true });
      if (url.endsWith('/cloud/orders/order-expired/events')) return response({
        items: renewed ? [{
          id: 'evt-renewed', sequence: 3, eventType: 'cloud.order.confirmation_renewed',
          entityType: 'cloud_order', entityId: 'order-expired',
          payload: { amount: '0.12', currency: 'CNY' }, createdAt: '2026-08-21T00:10:00Z',
        }] : [], total: renewed ? 1 : 0,
      });
      if (url.endsWith('/cloud/orders/order-expired/renew-confirmation') && init?.method === 'POST') {
        renewed = true;
        return response({
          ...expiredOrder,
          status: 'awaiting_confirmation',
          confirmationToken: 'renewed-confirmation-token-12345678901234567890',
          acknowledgement: '确认购买 tencent renew-me 每小时 0.12 CNY',
          confirmationExpiresAt: new Date(Date.now() + 30 * 60_000).toISOString(),
        });
      }
      if (url.endsWith('/cloud/orders/order-expired')) return response(renewed ? {
        ...expiredOrder,
        status: 'awaiting_confirmation',
        confirmationToken: 'renewed-confirmation-token-12345678901234567890',
        acknowledgement: '确认购买 tencent renew-me 每小时 0.12 CNY',
        confirmationExpiresAt: new Date(Date.now() + 30 * 60_000).toISOString(),
      } : expiredOrder);
      return response({ items: [] });
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('重新询价续签后恢复确认表单但不提交购买', async () => {
    renderOrder();
    expect(await screen.findByText('确认窗口已过期')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /重新验证并继续/ }));

    expect(await screen.findByRole('heading', { name: '二次确认' })).toBeInTheDocument();
    expect(screen.getByText('确认购买 tencent renew-me 每小时 0.12 CNY')).toBeInTheDocument();
    expect(screen.getByText('服务端已签发，本页面内有效')).toBeInTheDocument();
    expect(screen.queryByDisplayValue('renewed-confirmation-token-12345678901234567890')).not.toBeInTheDocument();
    expect(screen.getByLabelText('原样输入确认文本')).toHaveValue('');
    expect(await screen.findByText('确认窗口已重新签发')).toBeInTheDocument();

    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([request]) =>
      String(request).endsWith('/cloud/orders/order-expired/renew-confirmation'))).toBe(true));
    expect(vi.mocked(fetch).mock.calls.some(([request]) =>
      String(request).endsWith('/cloud/orders/order-expired/confirm'))).toBe(false);
  });
});
