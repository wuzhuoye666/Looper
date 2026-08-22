import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';

function response(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function catalog(resourceType: string, items: unknown[]) {
  return {
    provider: 'tencent',
    resourceType,
    items,
    total: items.length,
    source: 'live',
    fetchedAt: '2026-08-21T00:00:00Z',
    expiresAt: '2026-08-21T00:05:00Z',
    stale: false,
  };
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

describe('腾讯云购买网络选择', () => {
  beforeEach(() => {
    window.sessionStorage.clear();
    window.sessionStorage.setItem('looper.operator-token', 'operator-token-for-network-tests-1234567890');
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/cloud/providers')) return response({ items: [{
        id: 'tencent',
        name: '腾讯云 CVM',
        sdkPackage: 'tencentcloud-sdk-python-cvm + tencentcloud-sdk-python-vpc',
        sdkInstalled: true,
        credentialsConfigured: true,
        missingEnvironment: [],
        capabilities: ['regions', 'zones', 'instance-types', 'images', 'vpcs', 'subnets', 'security-groups', 'key-pairs', 'managed-security-group', 'hourly-quote', 'postpaid-purchase'],
        livePurchaseEnabled: true,
      }] });
      if (url.endsWith('/cloud/purchase-readiness')) return response({
        livePurchaseEnabled: true,
        operatorTokenReady: true,
        confirmationSecretReady: true,
        maxHourlyAmount: '1',
        providers: [{ provider: 'tencent', name: '腾讯云 CVM', ready: true, missingEnvironment: [], checks: [] }],
      });
      if (url.endsWith('/operator/session')) return response({ required: true, configured: true, authenticated: true, operatorGateReady: true });
      if (url.includes('/cloud/catalog/tencent/region')) return response(catalog('region', [
        { provider: 'tencent', id: 'ap-test', name: '测试地域', available: true },
      ]));
      if (url.includes('/cloud/catalog/tencent/zone')) return response(catalog('zone', [
        { provider: 'tencent', region: 'ap-test', id: 'ap-test-1', name: '测试一区', available: true },
      ]));
      if (url.includes('/cloud/catalog/tencent/instance-type')) return response(catalog('instance-type', []));
      if (url.includes('/cloud/catalog/tencent/vpc')) return response(catalog('vpc', [
        { provider: 'tencent', region: 'ap-test', id: 'vpc-default', name: 'Default-VPC', cidrBlock: '172.16.0.0/16', isDefault: true },
        { provider: 'tencent', region: 'ap-test', id: 'vpc-other', name: '业务网络', cidrBlock: '10.0.0.0/16', isDefault: false },
      ]));
      if (url.includes('/cloud/catalog/tencent/subnet')) {
        const selectedVpc = new URL(url).searchParams.get('vpc_id');
        return response(catalog('subnet', [{
          provider: 'tencent',
          region: 'ap-test',
          zone: 'ap-test-1',
          vpcId: selectedVpc,
          id: selectedVpc === 'vpc-other' ? 'subnet-other' : 'subnet-default',
          name: selectedVpc === 'vpc-other' ? '业务子网' : 'Default-Subnet',
          availableIpCount: 250,
          isDefault: true,
        }]));
      }
      if (url.includes('/cloud/catalog/tencent/security-group')) return response(catalog('security-group', [
        { provider: 'tencent', region: 'ap-test', id: 'sg-looper', name: 'looper-private', isDefault: false, recommended: true, tags: { managedBy: 'looper' } },
        { provider: 'tencent', region: 'ap-test', id: 'sg-other', name: 'other', isDefault: false, recommended: false, tags: {} },
      ]));
      if (url.includes('/cloud/catalog/tencent/key-pair')) return response(catalog('key-pair', [
        { provider: 'tencent', region: 'ap-test', id: 'skey-one', name: 'key-one', associatedInstanceCount: 0 },
        { provider: 'tencent', region: 'ap-test', id: 'skey-two', name: 'key-two', associatedInstanceCount: 0 },
      ]));
      return response({ items: [] });
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('自动选择默认网络和 Looper 安全组，并按 VPC 联动子网', async () => {
    renderMarket();
    await screen.findByRole('heading', { name: '云资源市场' });
    await screen.findByRole('option', { name: /测试地域/ });
    fireEvent.change(screen.getByLabelText('地域'), { target: { value: 'ap-test' } });
    await screen.findByRole('option', { name: /测试一区/ });
    fireEvent.change(screen.getByLabelText('可用区'), { target: { value: 'ap-test-1' } });

    expect(screen.getByRole('button', { name: /云资源选择/ })).toBeEnabled();
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([request]) =>
      String(request).includes('/cloud/catalog/tencent/vpc'))).toBe(true));
    const vpc = await screen.findByLabelText('私有网络 VPC *');
    await waitFor(() => expect(vpc).toHaveValue('vpc-default'));
    const subnet = screen.getByLabelText('子网 *');
    await waitFor(() => expect(subnet).toHaveValue('subnet-default'));
    expect(await screen.findByText('已选择 1 个安全组')).toBeInTheDocument();
    expect(screen.getByLabelText(/SSH 密钥/)).toHaveValue('');

    fireEvent.change(vpc, { target: { value: 'vpc-other' } });
    await waitFor(() => expect(subnet).toHaveValue('subnet-other'));
    expect(vi.mocked(fetch).mock.calls.some(([request]) => {
      const url = String(request);
      return url.includes('/cloud/catalog/tencent/subnet') && url.includes('vpc_id=vpc-other') && url.includes('zone=ap-test-1');
    })).toBe(true);
  });

  it('保留高级手动 ID 回退模式', async () => {
    renderMarket();
    await screen.findByRole('heading', { name: '云资源市场' });
    fireEvent.click(screen.getByRole('button', { name: /手动 ID/ }));
    expect(screen.getByLabelText('VPC ID *')).toBeInTheDocument();
    expect(screen.getByLabelText('子网 / vSwitch ID *')).toBeInTheDocument();
    expect(screen.getByLabelText('安全组 ID *')).toBeInTheDocument();
  });
});
