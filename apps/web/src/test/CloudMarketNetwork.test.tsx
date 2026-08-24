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

function catalog(resourceType: string, items: unknown[], total = items.length, offset = 0, nextOffset?: number, limit = 20) {
  return {
    provider: 'tencent',
    resourceType,
    items,
    total,
    offset,
    limit,
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
      if (url.endsWith('/cloud/ssh-defaults')) return response({ username: 'root', port: 22, authMethod: 'password', password: 'StrongPassword1#', passwordConfigured: true, privateKeyConfigured: true });
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
          typeLabel: '标准型',
          familyLabel: '标准型 S9',
          cpu: 4,
          memoryGib: 8,
          architecture: 'x86',
          available: true,
        }));
        return response(catalog('instance-type', all.slice(offset, offset + 20), all.length, offset, offset + 20 < all.length ? offset + 20 : undefined));
      }
      if (url.includes('/cloud/catalog/tencent/image')) {
        const params = new URL(url).searchParams;
        const offset = Number(params.get('offset') || 0);
        const limit = Number(params.get('limit') || 20);
        const all = Array.from({ length: 25 }, (_, index) => ({
          provider: 'tencent',
          region: 'ap-test',
          id: index === 0 ? 'img-tencentos-test' : `img-tencentos-test-${index + 1}`,
          name: index === 0 ? 'TencentOS Server 4 for x86_64'
            : index === 1 ? 'Custom Linux test image'
              : index === 2 ? 'Ubuntu Server 24.04 LTS 64位'
                : index === 3 ? 'Ubuntu Server 22.04 LTS 64位'
                  : index === 4 ? 'Debian 12.0 64位'
                    : index === 5 ? 'CentOS Stream 9 64位'
                      : index === 6 ? 'Windows Server 2022 数据中心版 64位 中文版'
                        : `TencentOS Server 4 test image ${index + 1}`,
          platform: 'TencentOS',
          architecture: 'x86_64',
          imageType: 'PUBLIC_IMAGE',
          sizeGib: 20,
          available: true,
        }));
        return response(catalog('image', all.slice(offset, offset + limit), all.length, offset, offset + limit < all.length ? offset + limit : undefined, limit));
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
        vpc: { provider: 'tencent', region: 'ap-test', id: 'vpc-default', name: 'Default-VPC', cidrBlock: '172.16.0.0/16', isDefault: true, tags: {}, managed: false },
        subnet: { provider: 'tencent', region: 'ap-test', zone: 'ap-test-1', vpcId: 'vpc-default', id: 'subnet-default', name: 'Default-Subnet', availableIpCount: 250, isDefault: true },
        securityGroup: { provider: 'tencent', region: 'ap-test', id: 'sg-looper', name: 'looper-ssh-access', isDefault: false, recommended: true, tags: { managedBy: 'looper', purpose: 'cloud-purchase', policyVersion: 'ssh-v1' }, managed: true },
        zoneAutomaticallySelected: true,
        vpcAction: 'reused',
        subnetAction: 'reused',
        securityGroupAction: 'created',
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
    const firstType = await screen.findByText('S9.TEST');
    expect(screen.getAllByText('标准型 · 标准型 S9').length).toBeGreaterThan(0);
    const firstRow = firstType.closest('tr');
    expect(firstRow).not.toBeNull();
    fireEvent.click(within(firstRow!).getByRole('button', { name: '选择并继续' }));
    await waitFor(
      () => expect(vi.mocked(fetch).mock.calls.some(([request]) =>
        String(request).includes('/cloud/network/tencent/resolve-instance-network'))).toBe(true),
      { timeout: 5000 },
    );
    await screen.findByRole('heading', { name: '购买草稿' }, { timeout: 5000 });
  }

  async function chooseFirstImage() {
    const image = await screen.findByLabelText('操作系统镜像');
    await waitFor(() => expect(image).not.toBeDisabled());
    fireEvent.change(image, { target: { value: 'img-tencentos-test' } });
  }

  it('按机型、兼容镜像、配置购买的顺序准备网络', async () => {
    renderMarket();
    await screen.findByRole('heading', { name: '云资源市场' });
    expect(await screen.findByRole('button', { name: /2.*配置与购买/ })).toBeDisabled();
    expect(screen.queryByRole('heading', { name: '购买草稿' })).not.toBeInTheDocument();

    await chooseFirstMachine();
    expect(vi.mocked(fetch).mock.calls.some(([request]) => String(request).includes('/cloud/network/tencent/resolve-instance-network'))).toBe(true);
    expect(screen.getAllByText(/ap-test-1/).length).toBeGreaterThan(0);
    expect(screen.getByText(/已复用 VPC Default-VPC · vpc-default/)).toBeInTheDocument();
    expect(screen.getByText(/已复用子网 Default-Subnet · subnet-default/)).toBeInTheDocument();
    expect(screen.getByText(/已创建安全组 looper-ssh-access · sg-looper/)).toBeInTheDocument();
    await waitFor(() => expect(vi.mocked(fetch).mock.calls.some(([request]) => {
      const url = String(request);
      return url.includes('/cloud/catalog/tencent/image') && url.includes('instance_type=S9.TEST') && url.includes('limit=500');
    })).toBe(true));
    expect(screen.getByLabelText('操作系统镜像')).toBeInTheDocument();

    await chooseFirstImage();
    const vpc = await screen.findByLabelText('私有网络 VPC *');
    await waitFor(() => expect(vpc).toHaveValue('vpc-default'));
    await waitFor(() => expect(screen.getByLabelText('子网 *')).toHaveValue('subnet-default'));
    expect(await screen.findByText('已选择 1 个安全组')).toBeInTheDocument();
    expect(screen.getByLabelText('SSH 登录方式 *')).toHaveValue('password');
    expect(screen.getByLabelText(/SSH 默认密码/)).toHaveValue('StrongPassword1#');
    expect(screen.queryByLabelText(/^SSH 密钥 \*/)).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('SSH 登录方式 *'), { target: { value: 'private-key' } });
    const keyPair = await screen.findByLabelText(/^SSH 密钥 \*/);
    await waitFor(() => expect(keyPair).toHaveValue('skey-one'));
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

  it('只恢复购买配置默认值并保留地域、机型和镜像', async () => {
    renderMarket();
    await chooseFirstMachine();
    await chooseFirstImage();
    await waitFor(() => expect(screen.getByLabelText('子网 *')).toHaveValue('subnet-default'));

    fireEvent.change(screen.getByLabelText('实例名称 *'), { target: { value: 'custom-instance' } });
    fireEvent.change(screen.getByLabelText('私有网络 VPC *'), { target: { value: 'vpc-other' } });
    await waitFor(() => expect(screen.getByLabelText('子网 *')).toHaveValue('subnet-other'));

    fireEvent.click(screen.getByRole('checkbox', { name: /other/ }));
    fireEvent.click(screen.getByRole('checkbox', { name: /sg-looper/ }));
    fireEvent.change(screen.getByLabelText('SSH 登录方式 *'), { target: { value: 'private-key' } });
    fireEvent.change(screen.getByLabelText('系统盘 GB'), { target: { value: '120' } });
    fireEvent.change(screen.getByLabelText('公网带宽 Mbps'), { target: { value: '12' } });
    fireEvent.click(screen.getByRole('checkbox', { name: /分配固定带宽公网 IP/ }));

    fireEvent.click(screen.getByRole('button', { name: '恢复默认设置' }));

    expect(screen.getByRole('heading', { name: '购买草稿' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '腾讯云 CVM · 机型' })).not.toBeInTheDocument();
    expect(screen.getAllByText(/ap-test-1/).length).toBeGreaterThan(0);
    expect(screen.getByLabelText('实例名称 *')).toHaveValue('looper-instance');
    expect(screen.getByLabelText('私有网络 VPC *')).toHaveValue('vpc-default');
    await waitFor(() => expect(screen.getByLabelText('子网 *')).toHaveValue('subnet-default'));
    expect(screen.getByRole('checkbox', { name: /sg-looper/ })).toBeChecked();
    expect(screen.getByRole('checkbox', { name: /other/ })).not.toBeChecked();
    expect(screen.getByLabelText('SSH 登录方式 *')).toHaveValue('password');
    expect(screen.getByLabelText('SSH 默认密码 *')).toHaveValue('StrongPassword1#');
    expect(screen.getByLabelText('系统盘 GB')).toHaveValue(50);
    expect(screen.getByRole('checkbox', { name: /分配固定带宽公网 IP/ })).toBeChecked();
    expect(screen.getByLabelText('公网带宽 Mbps')).toHaveValue(1);
    expect(screen.getByText('已选机型').parentElement).toHaveTextContent('S9.TEST');
    expect(screen.getByText('已选镜像').parentElement).toHaveTextContent('TencentOS Server 4 for x86_64');
  });

  it('手动目录分页且助手打开后重新从机型步骤开始', async () => {
    const view = renderMarket();
    await screen.findByRole('option', { name: /测试地域/ });
    expect(screen.getByLabelText('最低 vCPU')).toHaveValue(1);
    expect(screen.getByLabelText('最低内存 GiB')).toHaveValue(1);
    fireEvent.change(screen.getByLabelText('地域'), { target: { value: 'ap-test' } });
    await waitFor(() =>
      expect(view.container.querySelectorAll('.cloud-results tbody tr')).toHaveLength(20),
    );
    const firstType = await screen.findByText('S9.TEST');
    const firstRow = firstType.closest('tr');
    expect(view.container.querySelector('.cloud-instance-table')).toBeInTheDocument();
    expect(within(firstRow!).getByText('规格')).toHaveClass('instance-mobile-label');
    expect(within(firstRow!).getByText('架构')).toHaveClass('instance-mobile-label');
    expect(within(firstRow!).getByText('库存')).toHaveClass('instance-mobile-label');
    expect(within(firstRow!).getAllByRole('button')).toHaveLength(1);
    const search = screen.getByLabelText('搜索机型');
    const instanceRequests = () => vi.mocked(fetch).mock.calls
      .map(([request]) => String(request))
      .filter(url => url.includes('/cloud/catalog/tencent/instance-type'));
    const latestInstanceRequest = () => {
      const requests = instanceRequests();
      return requests[requests.length - 1];
    };
    const requestCountBeforeNumericTyping = instanceRequests().length;
    fireEvent.change(screen.getByLabelText('最低 vCPU'), { target: { value: '8' } });
    fireEvent.change(screen.getByLabelText('最低内存 GiB'), { target: { value: '16' } });
    expect(instanceRequests()).toHaveLength(requestCountBeforeNumericTyping);
    fireEvent.blur(screen.getByLabelText('最低 vCPU'));
    fireEvent.blur(screen.getByLabelText('最低内存 GiB'));
    await waitFor(() => expect(new URL(latestInstanceRequest()).searchParams.get('min_cpu')).toBe('8'));
    expect(new URL(latestInstanceRequest()).searchParams.get('min_memory_gib')).toBe('16');
    const requestCountBeforeTyping = instanceRequests().length;
    for (const value of ['S9.TEST.02', 'S9.TEST.0', 'S9.TEST.', 'S9.TEST', 'S9.TES']) {
      fireEvent.change(search, { target: { value } });
    }
    expect(search).toHaveValue('S9.TES');
    expect(instanceRequests()).toHaveLength(requestCountBeforeTyping);
    fireEvent.click(screen.getByRole('button', { name: '确认' }));
    await waitFor(() => expect(new URL(latestInstanceRequest()).searchParams.get('query')).toBe('S9.TES'));
    const requestCountAfterConfirm = instanceRequests().length;
    for (const value of ['S9.TE', 'S9.T', 'S9.', 'S9', 'S', '']) {
      fireEvent.change(search, { target: { value } });
    }
    expect(search).toHaveValue('');
    expect(instanceRequests()).toHaveLength(requestCountAfterConfirm);
    fireEvent.click(screen.getByRole('button', { name: '确认' }));
    await waitFor(() => expect(view.container.querySelectorAll('.cloud-results tbody tr')).toHaveLength(20));
    fireEvent.click(screen.getByRole('button', { name: '加载更多（已显示 20 / 25）' }));
    await waitFor(() => expect(view.container.querySelectorAll('.cloud-results tbody tr')).toHaveLength(25));

    expect(screen.queryByRole('button', { name: '打开选型助手' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: '腾讯云 CVM 选型助手' })).not.toBeInTheDocument();
  });

  it('配置页内联镜像选择器将常用系统置顶，其余收进更多镜像', async () => {
    renderMarket();
    await chooseFirstMachine();
    const image = screen.getByLabelText('操作系统镜像');
    expect(image).toBeInTheDocument();
    await waitFor(() => expect(image).not.toBeDisabled());
    const options = within(image).getAllByRole('option');
    expect(options).toHaveLength(8);
    expect(options[1]).toHaveTextContent('Ubuntu Server 24.04 LTS 64位');
    expect(options[2]).toHaveTextContent('Ubuntu Server 22.04 LTS 64位');
    expect(options[3]).toHaveTextContent('Debian 12.0 64位');
    expect(options[4]).toHaveTextContent('CentOS Stream 9 64位');
    expect(options[5]).toHaveTextContent('TencentOS Server 4 for x86_64');
    expect(options[6]).toHaveTextContent('Windows Server 2022');
    expect(options[7]).toHaveTextContent('更多镜像');
    expect(within(image).queryByRole('group', { name: '更多镜像' })).not.toBeInTheDocument();
    fireEvent.change(image, { target: { value: '__more_images__' } });
    expect(within(image).getByRole('group', { name: '更多镜像' })).toBeInTheDocument();
    expect(within(image).getByRole('option', { name: /Custom Linux test image/ })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '腾讯云 CVM · 镜像' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '选择并继续' })).not.toBeInTheDocument();
  });
});
