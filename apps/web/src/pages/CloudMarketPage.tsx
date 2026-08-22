import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Calculator,
  Check,
  CheckCircle2,
  ChevronDown,
  Cloud,
  Cpu,
  Image as ImageIcon,
  LockKeyhole,
  Network,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  ShoppingCart,
  Sparkles,
  Terminal,
  XCircle,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { AlibabaSelectionAdvisor } from '../components/AlibabaSelectionAdvisor';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';
import type {
  CloudImage,
  CloudInstanceType,
  CloudProviderId,
  CloudProviderReadiness,
  CloudPurchaseSpec,
  CloudQuote,
} from '../lib/types';

const providerLabels: Record<CloudProviderId, string> = {
  tencent: '腾讯云 CVM',
  alibaba: '阿里云 ECS',
  volcengine: '火山引擎 ECS',
  baidu: '百度智能云 BCC',
};
const kindLabels = { 'instance-type': '机型', image: '镜像' } as const;
type CatalogKind = keyof typeof kindLabels;
type NetworkMode = 'catalog' | 'manual';

function key() {
  return `looper-${Date.now()}-${window.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)}`;
}

function parseIds(value: string) {
  return [...new Set(value.split(',').map(item => item.trim()).filter(Boolean))].slice(0, 5);
}

export function CloudMarketPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const providers = useQuery({ queryKey: ['cloud-providers'], queryFn: api.providers, staleTime: 30_000 });
  const readiness = useQuery({ queryKey: ['cloud-purchase-readiness'], queryFn: api.purchaseReadiness, staleTime: 15_000 });
  const auth = useQuery({ queryKey: ['cloud-auth-status'], queryFn: api.cloudAuthStatus, staleTime: 15_000 });
  const available = providers.data?.items || [];
  const [provider, setProvider] = useState<CloudProviderId>('tencent');
  const [region, setRegion] = useState('');
  const [zone, setZone] = useState('');
  const [kind, setKind] = useState<CatalogKind>('instance-type');
  const [search, setSearch] = useState('');
  const [catalogSearch, setCatalogSearch] = useState('');
  const [minCpu, setMinCpu] = useState(0);
  const [minMemory, setMinMemory] = useState(0);
  const [selectedType, setSelectedType] = useState<CloudInstanceType | null>(null);
  const [selectedImage, setSelectedImage] = useState<CloudImage | null>(null);
  const [name, setName] = useState('looper-instance');
  const [networkMode, setNetworkMode] = useState<NetworkMode>('catalog');
  const [vpcId, setVpcId] = useState('');
  const [subnetId, setSubnetId] = useState('');
  const [securityGroupIds, setSecurityGroupIds] = useState<string[]>([]);
  const [keyPairId, setKeyPairId] = useState('');
  const [manualVpcId, setManualVpcId] = useState('');
  const [manualSubnetId, setManualSubnetId] = useState('');
  const [manualSecurityGroups, setManualSecurityGroups] = useState('');
  const [manualKeyPairId, setManualKeyPairId] = useState('');
  const [disk, setDisk] = useState(50);
  const [publicIp, setPublicIp] = useState(false);
  const [bandwidth, setBandwidth] = useState(0);
  const [quote, setQuote] = useState<CloudQuote | null>(null);
  const [quoteSignature, setQuoteSignature] = useState('');
  const currentSpecSignature = useRef('');
  const quoteKey = useRef(key());
  const orderKey = useRef(key());

  const providerInfo = available.find(item => item.id === provider);
  const providerReadiness = readiness.data?.providers.find(item => item.provider === provider);
  const operatorAccessReady = !auth.data?.required || auth.data.authenticated;
  const publicIpSupported = provider !== 'volcengine' && provider !== 'baidu';
  const quoteSupported = !providerInfo?.capabilities.includes('quote-blocked-price-mapping');
  const purchaseReady = Boolean(providerReadiness?.ready && operatorAccessReady);
  const networkCatalogSupported = Boolean(
    providerInfo?.capabilities.includes('vpcs') &&
    providerInfo.capabilities.includes('subnets') &&
    providerInfo.capabilities.includes('security-groups'),
  );
  const networkQueriesEnabled = networkMode === 'catalog' && networkCatalogSupported && operatorAccessReady;

  const regions = useQuery({
    queryKey: ['cloud-regions', provider],
    queryFn: () => api.regions(provider),
    enabled: !!providerInfo?.credentialsConfigured,
    staleTime: 300_000,
  });
  const zones = useQuery({
    queryKey: ['cloud-zones', provider, region],
    queryFn: () => api.zones(provider, region),
    enabled: !!region && !!providerInfo?.credentialsConfigured && !(provider === 'alibaba' && kind === 'instance-type'),
    staleTime: 300_000,
  });
  const catalog = useQuery({
    queryKey: ['cloud-catalog', provider, kind, region, zone, catalogSearch, minCpu, minMemory],
    queryFn: () => api.catalog<CloudInstanceType | CloudImage>(provider, kind, {
      region,
      zone: kind === 'instance-type' ? zone : undefined,
      query: catalogSearch,
      min_cpu: kind === 'instance-type' && minCpu ? minCpu : undefined,
      min_memory_gib: kind === 'instance-type' && minMemory ? minMemory : undefined,
      limit: 80,
    }),
    enabled: !!region && !!providerInfo?.credentialsConfigured,
    staleTime: 30_000,
  });
  const vpcs = useQuery({
    queryKey: ['cloud-vpcs', provider, region],
    queryFn: () => api.vpcs(provider, region),
    enabled: networkQueriesEnabled && !!region,
    staleTime: 30_000,
  });
  const subnets = useQuery({
    queryKey: ['cloud-subnets', provider, region, zone, vpcId],
    queryFn: () => api.subnets(provider, region, zone, vpcId),
    enabled: networkQueriesEnabled && !!region && !!zone && !!vpcId,
    staleTime: 30_000,
  });
  const securityGroups = useQuery({
    queryKey: ['cloud-security-groups', provider, region],
    queryFn: () => api.securityGroups(provider, region),
    enabled: networkQueriesEnabled && !!region,
    staleTime: 30_000,
  });
  const keyPairs = useQuery({
    queryKey: ['cloud-key-pairs', provider, region],
    queryFn: () => api.keyPairs(provider, region),
    enabled: networkQueriesEnabled && !!region && !!providerInfo?.capabilities.includes('key-pairs'),
    staleTime: 30_000,
  });
  const items = catalog.data?.items || [];
  const securityGroupItems = useMemo(
    () => [...(securityGroups.data?.items || [])].sort((left, right) =>
      Number(right.recommended) - Number(left.recommended) || left.name.localeCompare(right.name)),
    [securityGroups.data?.items],
  );

  const quoteMutation = useMutation({
    mutationFn: (request: { spec: CloudPurchaseSpec; key: string; signature: string }) =>
      api.quote(request.spec, request.key),
    onSuccess: (value, request) => {
      if (request.signature === currentSpecSignature.current) {
        setQuote(value);
        setQuoteSignature(request.signature);
      }
    },
  });
  const prepareMutation = useMutation({
    mutationFn: (quoteId: string) => api.prepareOrder(quoteId, orderKey.current),
    onSuccess: order => navigate(`/cloud/orders/${order.id}`, { state: order }),
  });
  const managedGroupMutation = useMutation({
    mutationFn: () => api.ensureManagedSecurityGroup(provider, region),
    onSuccess: group => {
      setSecurityGroupIds([group.id]);
      void queryClient.invalidateQueries({ queryKey: ['cloud-security-groups', provider, region] });
    },
  });

  useEffect(() => {
    const timer = window.setTimeout(() => setCatalogSearch(search.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [search]);
  useEffect(() => {
    setRegion('');
    setZone('');
    setSelectedType(null);
    setSelectedImage(null);
    setNetworkMode(provider === 'tencent' ? 'catalog' : 'manual');
    setVpcId('');
    setSubnetId('');
    setSecurityGroupIds([]);
    setKeyPairId('');
    setPublicIp(false);
    setQuote(null);
    setQuoteSignature('');
    quoteKey.current = key();
    orderKey.current = key();
  }, [provider]);
  useEffect(() => {
    setZone('');
    setSelectedType(null);
    setSelectedImage(null);
    setVpcId('');
    setSubnetId('');
    setSecurityGroupIds([]);
    setKeyPairId('');
    setQuote(null);
    setQuoteSignature('');
    quoteKey.current = key();
    orderKey.current = key();
  }, [region]);
  useEffect(() => {
    setSubnetId('');
    setSelectedType(null);
  }, [zone]);
  useEffect(() => {
    setSubnetId('');
  }, [vpcId]);

  useEffect(() => {
    const options = vpcs.data?.items;
    if (!options) return;
    if (vpcId && options.some(item => item.id === vpcId)) return;
    const defaults = options.filter(item => item.isDefault);
    const next = defaults.length === 1 ? defaults[0].id : options.length === 1 ? options[0].id : '';
    setVpcId(next);
  }, [vpcs.data?.items, vpcId]);
  useEffect(() => {
    const options = subnets.data?.items;
    if (!options) return;
    if (subnetId && options.some(item => item.id === subnetId)) return;
    const defaults = options.filter(item => item.isDefault);
    const next = defaults.length === 1 ? defaults[0].id : options.length === 1 ? options[0].id : '';
    setSubnetId(next);
  }, [subnets.data?.items, subnetId]);
  useEffect(() => {
    const options = securityGroups.data?.items;
    if (!options) return;
    const valid = securityGroupIds.filter(id => options.some(item => item.id === id));
    if (valid.length) {
      if (valid.length !== securityGroupIds.length) setSecurityGroupIds(valid);
      return;
    }
    const recommended = options.filter(item => item.recommended);
    const next = recommended.length === 1 ? [recommended[0].id] : options.length === 1 ? [options[0].id] : [];
    if (next.length || securityGroupIds.length) setSecurityGroupIds(next);
  }, [securityGroups.data?.items, securityGroupIds]);
  useEffect(() => {
    const options = keyPairs.data?.items;
    if (keyPairId && options && !options.some(item => item.id === keyPairId)) setKeyPairId('');
  }, [keyPairs.data?.items, keyPairId]);

  const effectiveVpcId = networkMode === 'catalog' ? vpcId : manualVpcId.trim();
  const effectiveSubnetId = networkMode === 'catalog' ? subnetId : manualSubnetId.trim();
  const effectiveSecurityGroups = networkMode === 'catalog'
    ? securityGroupIds
    : parseIds(manualSecurityGroups);
  const effectiveKeyPairId = networkMode === 'catalog' ? keyPairId : manualKeyPairId.trim();
  const spec = useMemo<CloudPurchaseSpec | null>(() => {
    if (!selectedType || !selectedImage || !region || !zone || !effectiveVpcId || !effectiveSubnetId || !effectiveSecurityGroups.length) return null;
    return {
      provider,
      region,
      zone,
      instanceType: selectedType.id,
      cpu: selectedType.cpu,
      memoryGib: selectedType.memoryGib,
      imageId: selectedImage.id,
      instanceName: name.trim() || 'looper-instance',
      count: 1,
      billingMode: 'postpaid',
      vpcId: effectiveVpcId,
      subnetId: effectiveSubnetId,
      securityGroupIds: effectiveSecurityGroups,
      keyPairId: effectiveKeyPairId || undefined,
      systemDiskGib: disk,
      publicIp,
      internetBandwidthMbps: publicIp ? bandwidth : 0,
      tags: { managedBy: 'looper' },
    };
  }, [
    provider,
    region,
    zone,
    selectedType,
    selectedImage,
    name,
    effectiveVpcId,
    effectiveSubnetId,
    effectiveSecurityGroups,
    effectiveKeyPairId,
    disk,
    publicIp,
    bandwidth,
  ]);
  const specSignature = useMemo(() => spec ? JSON.stringify(spec) : '', [spec]);
  currentSpecSignature.current = specSignature;
  const quoteMatchesCurrentSpec = Boolean(quote && quoteSignature === specSignature);
  useEffect(() => {
    setQuote(null);
    setQuoteSignature('');
    quoteKey.current = key();
    orderKey.current = key();
  }, [specSignature]);
  useEffect(() => {
    if (kind === 'instance-type' && selectedType) {
      const latest = (items as CloudInstanceType[]).find(item => item.id === selectedType.id);
      if (latest?.available === false) setSelectedType(null);
    }
    if (kind === 'image' && selectedImage) {
      const latest = (items as CloudImage[]).find(item => item.id === selectedImage.id);
      if (latest?.available === false) setSelectedImage(null);
    }
  }, [items, kind, selectedType, selectedImage]);

  const catalogError = vpcs.error || subnets.error || securityGroups.error || keyPairs.error;
  const hasRecommendedGroup = securityGroupItems.some(item => item.recommended);

  return <div className="page cloud-market-page">
    <PageHeader
      title="云资源市场"
      description="统一检索四家云的按量资源、实时询价与受保护订单。"
      actions={<button className="button secondary" onClick={() => Promise.all([providers.refetch(), readiness.refetch(), auth.refetch()])}><RefreshCw size={15} />刷新连接</button>}
    />
    <section className="provider-strip" aria-label="云厂商连接状态">
      {available.map(item => <button key={item.id} className={`provider-card ${provider === item.id ? 'selected' : ''}`} onClick={() => setProvider(item.id)}>
        <span className="provider-logo"><Cloud size={17} /></span>
        <span className="provider-card-copy"><strong>{item.name}</strong><small>{item.credentialsConfigured ? '凭证已配置' : '等待凭证'}</small></span>
        <span className={`connection-dot ${item.credentialsConfigured && item.sdkInstalled ? 'ready' : ''}`} />
      </button>)}
    </section>
    {providerReadiness && <PurchaseReadiness provider={providerReadiness} maxHourlyAmount={readiness.data?.maxHourlyAmount || '—'} authRequired={auth.data?.required || false} authenticated={auth.data?.authenticated || false} />}
    {providerInfo && !providerInfo.credentialsConfigured && <div className="notice warning"><AlertTriangle size={18} /><div><strong>{providerInfo.name} 尚未连接</strong><p>SDK 已安装；API 仅从服务端环境变量读取凭证。当前可查看能力和订单策略，实时目录需要配置：{providerInfo.missingEnvironment.join('、')}。</p></div></div>}
    {providerInfo?.credentialsConfigured && providerInfo.message && <div className="notice warning"><AlertTriangle size={18} /><div><strong>{providerInfo.name} 购买能力受限</strong><p>{providerInfo.message}</p></div></div>}
    {providerInfo?.credentialsConfigured && <>
      <section className="panel market-toolbar">
        <div className="field compact"><label htmlFor="market-region">地域</label><select id="market-region" value={region} onChange={event => setRegion(event.target.value)}><option value="">选择地域</option>{regions.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></div>
        <div className="field compact"><label htmlFor="market-zone">可用区</label><select id="market-zone" value={zone} onChange={event => setZone(event.target.value)} disabled={!region}><option value="">选择可用区</option>{zones.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></div>
        <div className="segmented" role="tablist" aria-label="资源类型"><button className={kind === 'instance-type' ? 'active' : ''} onClick={() => setKind('instance-type')}><Cpu size={15} />机型</button><button className={kind === 'image' ? 'active' : ''} onClick={() => setKind('image')}><ImageIcon size={15} />镜像</button></div>
        {kind === 'instance-type' && provider !== 'alibaba' && <><div className="field compact numeric-filter"><label htmlFor="min-cpu">最低 vCPU</label><input id="min-cpu" type="number" min={0} value={minCpu} onChange={event => setMinCpu(Number(event.target.value))} /></div><div className="field compact numeric-filter"><label htmlFor="min-memory">最低内存 GiB</label><input id="min-memory" type="number" min={0} step={0.5} value={minMemory} onChange={event => setMinMemory(Number(event.target.value))} /></div></>}
        {provider === 'alibaba' && kind === 'instance-type' ? <span className="advisor-toolbar-note"><Sparkles size={14} />使用下方助手逐步筛选</span> : <label className="search-field market-search"><Search size={16} /><span className="sr-only">搜索云资源</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder={`搜索${kindLabels[kind]}名称或 ID`} /></label>}
      </section>
      {provider === 'alibaba' && <div hidden={kind !== 'instance-type'}><AlibabaSelectionAdvisor regions={regions.data?.items || []} zones={zones.data?.items || []} region={region} zone={zone} onRegionChange={setRegion} onZoneChange={setZone} selected={selectedType} onSelect={setSelectedType} /></div>}
      {!(provider === 'alibaba' && kind === 'instance-type') && (catalog.isLoading ? <LoadingState /> : catalog.isError ? <ErrorState error={catalog.error} onRetry={() => catalog.refetch()} /> : items.length ? <section className="panel cloud-results"><div className="panel-heading"><div><h2>{providerLabels[provider]} · {kindLabels[kind]}</h2><p>{catalog.data?.source === 'stale-cache' ? catalog.data.warning : `${items.length} 个结果`}</p></div><span className="cache-state">{catalog.data?.source === 'live' ? '实时' : '缓存'}</span></div>{kind === 'instance-type' ? <InstanceTypeTable items={items as CloudInstanceType[]} selected={selectedType} onSelect={value => setSelectedType(value)} /> : <ImageTable items={items as CloudImage[]} selected={selectedImage} onSelect={value => setSelectedImage(value)} />}</section> : <EmptyState title="没有匹配的云资源" />)}
    </>}

    <section className="panel launch-panel">
      <div className="panel-heading"><div><h2>购买草稿</h2><p>仅按量付费；报价不锁定库存，创建前仍需服务端确认。</p></div><ShieldCheck size={20} /></div>
      <div className="form-grid cloud-form">
        <label><span>实例名称 *</span><input value={name} onChange={event => setName(event.target.value)} /></label>
        <div className="network-mode-row full">
          <span><Network size={16} /><strong>网络与访问</strong></span>
          <div className="segmented network-mode" role="group" aria-label="网络配置方式">
            <button type="button" className={networkMode === 'catalog' ? 'active' : ''} disabled={!networkCatalogSupported} onClick={() => setNetworkMode('catalog')}><Network size={14} />云资源选择</button>
            <button type="button" className={networkMode === 'manual' ? 'active' : ''} onClick={() => setNetworkMode('manual')}><Settings2 size={14} />手动 ID</button>
          </div>
        </div>

        {networkMode === 'catalog' ? <>
          <label><span>私有网络 VPC *</span><select id="launch-vpc" value={vpcId} disabled={!networkQueriesEnabled || !region || vpcs.isLoading} onChange={event => setVpcId(event.target.value)}><option value="">{!operatorAccessReady ? '需要操作员认证' : vpcs.isLoading ? '正在读取 VPC…' : '选择 VPC'}</option>{vpcs.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}{item.isDefault ? ' · 默认' : ''}{item.cidrBlock ? ` · ${item.cidrBlock}` : ''}</option>)}</select></label>
          <label><span>子网 *</span><select id="launch-subnet" value={subnetId} disabled={!networkQueriesEnabled || !zone || !vpcId || subnets.isLoading} onChange={event => setSubnetId(event.target.value)}><option value="">{!zone ? '先选择可用区' : !vpcId ? '先选择 VPC' : subnets.isLoading ? '正在读取子网…' : '选择子网'}</option>{subnets.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}{item.isDefault ? ' · 默认' : ''}{item.availableIpCount !== undefined ? ` · ${item.availableIpCount} 个可用 IP` : ''}</option>)}</select></label>
          <div className="security-group-field full">
            <span className="field-label">安全组 *</span>
            <details className="security-group-picker">
              <summary><ShieldCheck size={15} /><span>{securityGroupIds.length ? `已选择 ${securityGroupIds.length} 个安全组` : securityGroups.isLoading ? '正在读取安全组…' : '选择安全组'}</span><ChevronDown size={15} /></summary>
              <div className="security-group-options">
                {securityGroupItems.map(item => <label key={item.id} className={securityGroupIds.includes(item.id) ? 'selected' : ''}><input type="checkbox" checked={securityGroupIds.includes(item.id)} disabled={!securityGroupIds.includes(item.id) && securityGroupIds.length >= 5} onChange={event => setSecurityGroupIds(current => event.target.checked ? [...new Set([...current, item.id])].slice(0, 5) : current.filter(id => id !== item.id))} /><span><strong>{item.name}{item.recommended ? ' · Looper 推荐' : item.isDefault ? ' · 默认' : ''}</strong><small>{item.id}{item.description ? ` · ${item.description}` : ''}</small></span></label>)}
                {!securityGroups.isLoading && !securityGroupItems.length && <div className="network-empty">当前地域没有可用安全组</div>}
              </div>
            </details>
            {!hasRecommendedGroup && region && operatorAccessReady && <button type="button" className="button secondary compact-button managed-group-button" disabled={managedGroupMutation.isPending} onClick={() => managedGroupMutation.mutate()}><Plus size={14} />{managedGroupMutation.isPending ? '创建中…' : '创建 Looper 安全组'}</button>}
          </div>
          <label><span>SSH 密钥</span><select id="launch-key-pair" value={keyPairId} disabled={!networkQueriesEnabled || !region || keyPairs.isLoading} onChange={event => setKeyPairId(event.target.value)}><option value="">不设置 SSH 密钥</option>{keyPairs.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select><small>可选；存在多个密钥时不会自动选择。</small></label>
          {catalogError && <div className="network-catalog-error full"><AlertTriangle size={16} /><span>云网络目录读取失败。</span><button type="button" onClick={() => setNetworkMode('manual')}>改用手动 ID</button></div>}
          {managedGroupMutation.isError && <div className="inline-error full">{managedGroupMutation.error instanceof Error ? managedGroupMutation.error.message : '安全组创建失败'}</div>}
        </> : <>
          <label><span>VPC ID *</span><input value={manualVpcId} onChange={event => setManualVpcId(event.target.value)} placeholder="vpc-..." /></label>
          <label><span>子网 / vSwitch ID *</span><input value={manualSubnetId} onChange={event => setManualSubnetId(event.target.value)} placeholder="subnet-..." /></label>
          <label><span>安全组 ID *</span><input value={manualSecurityGroups} onChange={event => setManualSecurityGroups(event.target.value)} placeholder="最多 5 个，用逗号分隔" /></label>
          <label><span>SSH 密钥 ID</span><input value={manualKeyPairId} onChange={event => setManualKeyPairId(event.target.value)} placeholder="可选" /></label>
        </>}

        <label><span>系统盘 GB</span><input type="number" min={20} max={2048} value={disk} onChange={event => setDisk(Number(event.target.value))} /></label>
        <label className="checkbox-field"><input type="checkbox" checked={publicIp} disabled={!publicIpSupported} onChange={event => setPublicIp(event.target.checked)} /><span>{publicIpSupported ? '分配固定带宽公网 IP' : '公网 IP 需独立定价流程'}</span></label>
        <label><span>公网带宽 Mbps</span><input type="number" min={0} max={1000} disabled={!publicIp} value={bandwidth} onChange={event => setBandwidth(Number(event.target.value))} /></label>
      </div>
      <div className="launch-summary"><div><span>已选机型</span><strong>{selectedType ? `${selectedType.id} · ${selectedType.cpu} vCPU / ${selectedType.memoryGib} GiB` : '未选择'}</strong></div><div><span>已选镜像</span><strong>{selectedImage ? selectedImage.name : '未选择'}</strong></div><button className="button primary" disabled={!spec || !quoteSupported || quoteMutation.isPending || !operatorAccessReady} onClick={() => spec && quoteMutation.mutate({ spec, key: quoteKey.current, signature: specSignature })}><Calculator size={16} />{!operatorAccessReady ? '需要操作员认证' : !quoteSupported ? '报价配置未完成' : quoteMutation.isPending ? '询价中...' : '获取小时报价'}</button></div>
      {quoteMutation.isError && <div className="inline-error">{quoteMutation.error instanceof Error ? quoteMutation.error.message : '询价失败'}</div>}
      {quote && quoteMatchesCurrentSpec && <div className="quote-card"><div><span>报价快照</span><strong>{quote.hourlyAmount} {quote.currency}<small> / 小时{quote.estimated ? ' · 预计' : ''}</small></strong><em>{providerLabels[quote.provider]} · {quote.spec.region} · {quote.spec.instanceType} · {quote.spec.imageId} · {quote.spec.count} 台</em><em>有效至 {new Date(quote.expiresAt).toLocaleString()}</em></div><button className="button primary" disabled={prepareMutation.isPending || quote.estimated || !quoteMatchesCurrentSpec || !purchaseReady} onClick={() => quoteMatchesCurrentSpec && purchaseReady && prepareMutation.mutate(quote.id)}><ShoppingCart size={16} />{quote.estimated ? '估算价不可购买' : !purchaseReady ? '购买门禁未就绪' : prepareMutation.isPending ? '准备订单...' : '进入确认'}</button></div>}
      {prepareMutation.isError && <div className="inline-error">{prepareMutation.error instanceof Error ? prepareMutation.error.message : '订单准备失败'}</div>}
    </section>
  </div>;
}

function PurchaseReadiness({ provider, maxHourlyAmount, authRequired, authenticated }: { provider: CloudProviderReadiness; maxHourlyAmount: string; authRequired: boolean; authenticated: boolean }) {
  const browserCheck = { code: 'browser-auth', label: '浏览器操作员', ready: !authRequired || authenticated, detail: authRequired ? authenticated ? '当前会话已认证' : '点击顶部钥匙并输入 Operator token' : '服务器尚未要求认证' };
  const checks = [...provider.checks, browserCheck];
  const ready = provider.ready && browserCheck.ready;
  const configurable = provider.provider === 'tencent' || provider.provider === 'alibaba';
  return <section className={`purchase-readiness ${ready ? 'ready' : 'blocked'}`} aria-label={`${provider.name}购买就绪状态`}>
    <div className="purchase-readiness-heading"><div className="readiness-title-icon">{ready ? <CheckCircle2 size={20} /> : <LockKeyhole size={20} />}</div><div><span className="eyebrow">LIVE PURCHASE</span><h2>{ready ? `${provider.name} 可以购买` : `${provider.name} 尚不可购买`}</h2><p>单笔总小时金额上限 {maxHourlyAmount} CNY</p></div></div>
    <div className="readiness-grid">{checks.map(check => <div key={check.code} className={check.ready ? 'ready' : 'blocked'}>{check.ready ? <CheckCircle2 size={15} /> : <XCircle size={15} />}<span><strong>{check.label}</strong><small>{check.detail}</small></span></div>)}</div>
    {!ready && configurable && <div className="setup-command"><Terminal size={16} /><span><strong>本机配置命令</strong><code>.venv\Scripts\looper.exe cloud configure {provider.provider} --max-hourly-amount {maxHourlyAmount === '—' ? '10' : maxHourlyAmount}</code></span></div>}
  </section>;
}

function InstanceTypeTable({ items, selected, onSelect }: { items: CloudInstanceType[]; selected: CloudInstanceType | null; onSelect: (value: CloudInstanceType) => void }) {
  return <div className="table-wrap"><table><thead><tr><th>机型</th><th>规格</th><th>架构</th><th>库存提示</th><th /></tr></thead><tbody>{items.map(item => <tr key={item.id} className={selected?.id === item.id ? 'selected-row' : ''}><td><strong>{item.id}</strong><span className="cell-meta">{item.family || '通用型'}</span></td><td>{item.cpu} vCPU · {item.memoryGib} GiB</td><td>{item.architecture || '—'}</td><td><span className={`stock-label ${item.available === true ? 'available' : item.available === false ? 'unavailable' : 'unknown'}`}>{item.available === true ? '可用' : item.available === false ? '不足' : '未知'}</span></td><td><button className="button secondary compact-button" disabled={item.available === false} onClick={() => onSelect(item)}>{item.available === false ? '不可用' : selected?.id === item.id ? <><Check size={14} />已选</> : '选择'}</button></td></tr>)}</tbody></table></div>;
}

function ImageTable({ items, selected, onSelect }: { items: CloudImage[]; selected: CloudImage | null; onSelect: (value: CloudImage) => void }) {
  return <div className="table-wrap"><table><thead><tr><th>镜像</th><th>平台</th><th>架构</th><th>大小</th><th /></tr></thead><tbody>{items.map(item => <tr key={item.id} className={selected?.id === item.id ? 'selected-row' : ''}><td><strong>{item.name}</strong><span className="cell-meta">{item.id}</span></td><td>{item.platform || '—'}</td><td>{item.architecture || '—'}</td><td>{item.sizeGib ? `${item.sizeGib} GiB` : '—'}</td><td><button className="button secondary compact-button" disabled={item.available === false} onClick={() => onSelect(item)}>{item.available === false ? '不可用' : selected?.id === item.id ? <><Check size={14} />已选</> : '选择'}</button></td></tr>)}</tbody></table></div>;
}
