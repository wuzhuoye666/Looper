import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../App';

function response(data: unknown) {
  return new Response(JSON.stringify(data), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  });
}

function catalog(resourceType: string, items: unknown[], total = items.length, offset = 0, nextOffset?: number) {
  return {
    provider: 'tencent',
    resourceType,
    items,
    total,
    offset,
    limit: 20,
    nextOffset,
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
      if (url.includes('/cloud/catalog/tencent/instance-type')) {
        const offset = Number(new URL(url).searchParams.get('offset') || 0);
        const all = Array.from({ length: 25 }, (_, index) => ({
          provider: 'tencent',
          region: 'ap-test',
          zone: 'ap-test-1',
          id: index === 0 ? 'S9.TEST' : `S9.TEST.${String(index + 1).padStart(2, '0')}`,
          family: 'S9',
          cpu: 4,
          memoryGib: 8,
          architecture: 'x86',
          available: true,
        }));
        return response(catalog('instance-type', all.slice(offset, offset + 20), all.length, offset, offset + 20 < all.length ? offset + 20 : undefined));
      }
      if (url.includes('/cloud/catalog/tencent/image')) {
        const offset = Number(new URL(url).searchParams.get('offset') || 0);
        const all = Array.from({ length: 25 }, (_, index) => ({
          provider: 'tencent',
          region: 'ap-test',
          id: index === 0 ? 'img-tencentos-test' : `img-tencentos-test-${index + 1}`,
          name: index === 0 ? 'TencentOS Server 4 for x86_64' : `TencentOS Server 4 test image ${index + 1}`,
          platform: 'TencentOS',
          architecture: 'x86_64',
          imageType: 'PUBLIC_IMAGE',
          sizeGib: 20,
          available: true,
        }));
        return response(catalog('image', all.slice(offset, offset + 20), all.length, offset, offset + 20 < all.length ? offset + 20 : undefined));
      }
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
      if (url.includes('/cloud/network/tencent/resolve-instance-network')) return response({
        provider: 'tencent',
        region: 'ap-test',
        instanceType: 'S9.TEST',
        zone: 'ap-test-1',
        eligibleZones: ['ap-test-1'],
        vpc: { provider: 'tencent', region: 'ap-test', id: 'vpc-default', name: 'Default-VPC', cidrBlock: '172.16.0.0/16', isDefault: true },
        subnet: { provider: 'tencent', region: 'ap-test', zone: 'ap-test-1', vpcId: 'vpc-default', id: 'subnet-default', name: 'Default-Subnet', availableIpCount: 250, isDefault: true },
        zoneAutomaticallySelected: true,
        subnetAction: 'reused',
        warnings: [],
      });
      if (url.includes('/cloud/selection-advisor/search')) return response({
        provider: 'tencent',
        region: 'ap-test',
        items: [{
          provider: 'tencent', region: 'ap-test', id: 'S9.ADVISOR', family: 'S9', cpu: 8, memoryGib: 16,
          architecture: 'X86', zones: ['ap-test-1'], available: true, matchTier: 'preferred',
          reasons: ['规格族适合 Web / API 场景'], warnings: [],
        }],
        total: 1,
        eligibleTotal: 1,
        offset: 0,
        limit: 20,
        nextOffset: null,
        exclusionStages: [{ code: 'availability', label: '可用区库存', before: 1, after: 1, removed: 0 }],
        source: 'live',
        fetchedAt: '2026-08-21T00:00:00Z',
        expiresAt: '2026-08-21T00:05:00Z',
        stale: false,
      });
      return response({ items: [] });
    }));
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  async function chooseFirstMachine() {
    await screen.findByRole('option', { name: /测试地域/ });
    fireEvent.change(screen.getByLabelText('地域'), { target: { value: 'ap-test' } });
    await waitFor(() =>
      expect(document.querySelectorAll('.cloud-instance-table tbody tr')).toHaveLength(20),
    );
    const table = document.querySelector<HTMLElement>('.cloud-instance-table')!;
    const firstType = within(table).getByText('S9.TEST');
    fireEvent.click(within(firstType.closest('tr')!).getByRole('button', { name: '选择并继续' }));
    await waitFor(
      () => expect(vi.mocked(fetch).mock.calls.some(([request]) =>
        String(request).includes('/cloud/network/tencent/resolve-instance-network'))).toBe(true),
      { timeout: 5000 },
    );
    await screen.findByRole('heading', { name: '腾讯云 CVM · 镜像' }, { timeout: 5000 });
  }

  async function chooseFirstImage() {
    const imageName = await screen.findByText('TencentOS Server 4 for x86_64');
    fireEvent.click(within(imageName.closest('tr')!).getByRole('button', { name: '选择并继续' }));
    await screen.findByRole('heading', { name: '购买草稿' });
  }

  it('按机型、兼容镜像、配置购买的顺序准备网络', async () => {
    renderMarket();
    await screen.findByRole('heading', { name: '云资源市场' });
    expect(await screen.findByRole('button', { name: /2.*选择镜像/ })).toBeDisabled();
    expect(screen.queryByRole('heading', { name: '购买草稿' })).not.toBeInTheDocument();

    await chooseFirstMachine();
    expect(vi.mocked(fetch).mock.calls.some(([request]) => String(request).includes('/cloud/network/tencent/resolve-instance-network'))).toBe(true);
    expect(screen.getAllByText(/ap-test-1/).length).toBeGreaterThan(0);
    expect(vi.mocked(fetch).mock.calls.some(([request]) => {
      const url = String(request);
      return url.includes('/cloud/catalog/tencent/image') && url.includes('instance_type=S9.TEST');
    })).toBe(true);

    await chooseFirstImage();
    const vpc = await screen.findByLabelText('私有网络 VPC *');
    await waitFor(() => expect(vpc).toHaveValue('vpc-default'));
    await waitFor(() => expect(screen.getByLabelText('子网 *')).toHaveValue('subnet-default'));
    expect(await screen.findByText('已选择 1 个安全组')).toBeInTheDocument();
    expect(screen.getByText('已选机型').parentElement).toHaveTextContent('S9.TEST');
    expect(screen.getByText('已选镜像').parentElement).toHaveTextContent('TencentOS Server 4 for x86_64');
  });

  it('在配置步骤保留高级手动 ID 回退模式', async () => {
    renderMarket();
    await chooseFirstMachine();
    await chooseFirstImage();
    fireEvent.click(screen.getByRole('button', { name: /手动 ID/ }));
    expect(screen.getByLabelText('VPC ID *')).toBeInTheDocument();
    expect(screen.getByLabelText('子网 / vSwitch ID *')).toBeInTheDocument();
    expect(screen.getByLabelText('安全组 ID *')).toBeInTheDocument();
  });

  it('手动目录分页且助手打开后重新从机型步骤开始', async () => {
    const view = renderMarket();
    await screen.findByRole('option', { name: /测试地域/ });
    fireEvent.change(screen.getByLabelText('地域'), { target: { value: 'ap-test' } });
    await waitFor(() =>
      expect(view.container.querySelectorAll('.cloud-results tbody tr')).toHaveLength(20),
    );
    const results = view.container.querySelector<HTMLElement>('.cloud-results')!;
    const firstType = within(results).getByText('S9.TEST');
    const firstRow = firstType.closest('tr');
    expect(view.container.querySelector('.cloud-instance-table')).toBeInTheDocument();
    expect(within(firstRow!).getByText('规格')).toHaveClass('instance-mobile-label');
    expect(within(firstRow!).getByText('架构')).toHaveClass('instance-mobile-label');
    expect(within(firstRow!).getByText('库存')).toHaveClass('instance-mobile-label');
    expect(within(firstRow!).getAllByRole('button')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: '加载更多（已显示 20 / 25）' }));
    await waitFor(() => expect(view.container.querySelectorAll('.cloud-results tbody tr')).toHaveLength(25));

    fireEvent.click(screen.getByRole('button', { name: '打开选型助手' }));
    expect(screen.getByRole('button', { name: '返回手动选型' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByLabelText('腾讯云 CVM 选型助手')).toBeInTheDocument();
    expect(screen.queryByLabelText('最低 vCPU')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '返回手动选型' }));
    expect(screen.queryByLabelText('腾讯云 CVM 选型助手')).not.toBeInTheDocument();
    expect(screen.getByLabelText('最低 vCPU')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '打开选型助手' }));
    expect(screen.getByRole('heading', { name: '主要使用场景是什么？' })).toBeInTheDocument();
  });

  it('兼容镜像列表保留窄屏卡片标记和分批展示', async () => {
    const view = renderMarket();
    await chooseFirstMachine();
    const imageName = await screen.findByText('TencentOS Server 4 for x86_64');
    const row = imageName.closest('tr');
    expect(row).not.toBeNull();
    expect(view.container.querySelector('.cloud-image-table')).toBeInTheDocument();
    expect(view.container.querySelectorAll('.cloud-image-table tbody tr')).toHaveLength(20);
    expect(within(row!).getAllByRole('button')).toHaveLength(1);
    expect(within(row!).getByText('平台')).toHaveClass('image-mobile-label');
    expect(within(row!).getByText('架构')).toHaveClass('image-mobile-label');
    expect(within(row!).getByText('大小')).toHaveClass('image-mobile-label');
    fireEvent.click(screen.getByRole('button', { name: '加载更多（已显示 20 / 25）' }));
    await waitFor(() => expect(view.container.querySelectorAll('.cloud-image-table tbody tr')).toHaveLength(25));
  });
});
