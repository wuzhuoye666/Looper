import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Calculator,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
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
  Upload,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { CloudSelectionAdvisor } from '../components/CloudSelectionAdvisor';
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
type PurchaseAuthMethod = 'password' | 'private-key';
type MarketStep = 'instance' | 'image' | 'configure';
const CATALOG_PAGE_SIZE = 20;
const DEFAULT_INSTANCE_NAME = 'looper-instance';
const DEFAULT_SYSTEM_DISK_GIB = 50;
const DEFAULT_PUBLIC_BANDWIDTH_MBPS = 1;

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
  const [step, setStep] = useState<MarketStep>('instance');
  const [advisorOpen, setAdvisorOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [catalogSearch, setCatalogSearch] = useState('');
  const [minCpu, setMinCpu] = useState(0);
  const [minMemory, setMinMemory] = useState(0);
  const [selectedType, setSelectedType] = useState<CloudInstanceType | null>(null);
  const [selectedImage, setSelectedImage] = useState<CloudImage | null>(null);
  const [defaultTypeId, setDefaultTypeId] = useState('');
  const [defaultImageId, setDefaultImageId] = useState('');
  const [suppressTypeDefault, setSuppressTypeDefault] = useState(false);
  const [name, setName] = useState(DEFAULT_INSTANCE_NAME);
  const [networkMode, setNetworkMode] = useState<NetworkMode>('catalog');
  const [vpcId, setVpcId] = useState('');
  const [subnetId, setSubnetId] = useState('');
  const [securityGroupIds, setSecurityGroupIds] = useState<string[]>([]);
  const [keyPairId, setKeyPairId] = useState('');
  const [manualVpcId, setManualVpcId] = useState('');
  const [manualSubnetId, setManualSubnetId] = useState('');
  const [manualSecurityGroups, setManualSecurityGroups] = useState('');
  const [manualKeyPairId, setManualKeyPairId] = useState('');
  const [disk, setDisk] = useState(DEFAULT_SYSTEM_DISK_GIB);
  const [publicIp, setPublicIp] = useState(false);
  const [bandwidth, setBandwidth] = useState(0);
  const [sshUsername, setSshUsername] = useState('root');
  const [sshAuthMethod, setSshAuthMethod] = useState<PurchaseAuthMethod>('private-key');
  const [sshPassword, setSshPassword] = useState('');
  const [sshPrivateKey, setSshPrivateKey] = useState('');
  const [rememberSshCredentials, setRememberSshCredentials] = useState(true);
  const [sshKeyFileName, setSshKeyFileName] = useState('');
  const [quote, setQuote] = useState<CloudQuote | null>(null);
  const [quoteSignature, setQuoteSignature] = useState('');
  const [networkNotice, setNetworkNotice] = useState('');
  const [selectionError, setSelectionError] = useState('');
  const currentSpecSignature = useRef('');
  const quoteKey = useRef(key());
  const orderKey = useRef(key());
  const networkKey = useRef(key());
  const kind: CatalogKind = step === 'instance' ? 'instance-type' : 'image';

  const providerInfo = available.find(item => item.id === provider);
  const selectionAdvisorSupported = provider === 'alibaba' || provider === 'tencent';
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
    enabled: !!region && !!providerInfo?.credentialsConfigured,
    staleTime: 300_000,
  });
  const catalog = useInfiniteQuery({
    queryKey: ['cloud-catalog', provider, kind, region, zone, selectedType?.id, catalogSearch, minCpu, minMemory],
    queryFn: ({ pageParam }) => api.catalog<CloudInstanceType | CloudImage>(provider, kind, {
      region,
      zone: kind === 'instance-type' ? zone : undefined,
      instance_type: kind === 'image' ? selectedType?.id : undefined,
      query: catalogSearch,
      min_cpu: kind === 'instance-type' && minCpu ? minCpu : undefined,
      min_memory_gib: kind === 'instance-type' && minMemory ? minMemory : undefined,
      offset: pageParam,
      limit: CATALOG_PAGE_SIZE,
    }),
    initialPageParam: 0,
    getNextPageParam: lastPage => lastPage.nextOffset ?? undefined,
    enabled: step !== 'configure' && !!region && !!providerInfo?.credentialsConfigured &&
      (step !== 'image' || !!selectedType) && !(selectionAdvisorSupported && advisorOpen && step === 'instance'),
    staleTime: 30_000,
  });
  const defaultImages = useQuery({
    queryKey: ['cloud-default-images', provider, region],
    queryFn: () => api.images(provider, { region, limit: 80 }),
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
  const catalogPages = catalog.data?.pages || [];
  const catalogResult = catalogPages[0];
  const items = catalogPages.flatMap(page => page.items);
  const displayedCatalogCount = items.length;
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
  const purchaseMutation = useMutation({
    mutationFn: (request: { quoteId: string; credentials: { username: string; port: number; authMethod: PurchaseAuthMethod; password?: string; privateKey?: string; passphrase?: string; rememberCredentials: boolean } }) => api.purchaseQuote(request.quoteId, orderKey.current, { sshCredentials: request.credentials }),
    onSuccess: order => {
      void queryClient.invalidateQueries({ queryKey: ['targets'] });
      navigate(`/cloud/orders/${order.id}`, { state: order });
    },
  });
  const managedGroupMutation = useMutation({
    mutationFn: () => api.ensureManagedSecurityGroup(provider, region),
    onSuccess: group => {
      setSecurityGroupIds([group.id]);
      void queryClient.invalidateQueries({ queryKey: ['cloud-security-groups', provider, region] });
    },
  });
  const networkMutation = useMutation({
    mutationFn: (instance: CloudInstanceType) => api.resolveInstanceNetwork(provider, {
      region,
      instanceType: instance.id,
      zone: zone || undefined,
      vpcId: vpcId || undefined,
      subnetId: subnetId || undefined,
    }, networkKey.current),
    onSuccess: (resolution, instance) => {
      setSelectedType(instance);
      setSelectedImage(null);
      setZone(resolution.zone);
      setVpcId(resolution.vpc.id);
      setSubnetId(resolution.subnet.id);
      setNetworkNotice(`${resolution.zoneAutomaticallySelected ? `已选择可售可用区 ${resolution.zone}` : `可用区 ${resolution.zone}`}；${resolution.subnetAction === 'created' ? '已创建' : '已复用'}子网 ${resolution.subnet.name} · ${resolution.subnet.id}`);
      setSelectionError('');
      setSearch('');
      setStep('image');
    },
    onError: error => setSelectionError(error instanceof Error ? error.message : '网络准备失败'),
  });

  useEffect(() => {
    const timer = window.setTimeout(() => setCatalogSearch(search.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [search]);
  useEffect(() => {
    setAdvisorOpen(false);
    setStep('instance');
    setRegion('');
    setZone('');
    setSelectedType(null);
    setSelectedImage(null);
    setDefaultTypeId('');
    setDefaultImageId('');
    setSuppressTypeDefault(false);
    setNetworkMode(provider === 'tencent' || provider === 'alibaba' ? 'catalog' : 'manual');
    setVpcId('');
    setSubnetId('');
    setSecurityGroupIds([]);
    setKeyPairId('');
    setPublicIp(publicIpSupported);
    setDisk(DEFAULT_SYSTEM_DISK_GIB);
    setBandwidth(DEFAULT_PUBLIC_BANDWIDTH_MBPS);
    setQuote(null);
    setQuoteSignature('');
    quoteKey.current = key();
    orderKey.current = key();
    networkKey.current = key();
    setNetworkNotice('');
    setSelectionError('');
  }, [provider, publicIpSupported]);
  useEffect(() => {
    setStep('instance');
    setZone('');
    setSelectedType(null);
    setSelectedImage(null);
    setDefaultTypeId('');
    setDefaultImageId('');
    setSuppressTypeDefault(false);
    setVpcId('');
    setSubnetId('');
    setSecurityGroupIds([]);
    setKeyPairId('');
    setQuote(null);
    setQuoteSignature('');
    quoteKey.current = key();
    orderKey.current = key();
    networkKey.current = key();
    setNetworkNotice('');
    setSelectionError('');
  }, [region]);
  useEffect(() => {
    const options = regions.data?.items;
    if (!options || region) return;
    const preferred = options.find(item => item.available !== false) || options[0];
    if (preferred) setRegion(preferred.id);
  }, [regions.data?.items, region]);

  useEffect(() => {
    const options = zones.data?.items;
    if (!options || zone) return;
    const preferred = options.find(item => item.available !== false) || options[0];
    if (preferred) setZone(preferred.id);
  }, [zones.data?.items, zone]);

  useEffect(() => {
    if (kind !== 'instance-type' || advisorOpen || suppressTypeDefault || selectedType || !items.length) return;
    const preferred = (items as CloudInstanceType[]).find(item => item.available !== false && item.attributes?.purchaseCompatible !== false);
    if (preferred) {
      setSelectedType(preferred);
      setDefaultTypeId(preferred.id);
    }
  }, [advisorOpen, items, kind, selectedType, suppressTypeDefault]);

  useEffect(() => {
    const imageItems = kind === 'image' ? (items as CloudImage[]) : defaultImages.data?.items || [];
    if (selectedImage || !imageItems.length) return;
    const preferred = imageItems.find(item => item.available !== false);
    if (preferred) {
      setSelectedImage(preferred);
      setDefaultImageId(preferred.id);
    }
  }, [defaultImages.data?.items, items, kind, selectedImage]);

  useEffect(() => {
    const options = vpcs.data?.items;
    if (!options) return;
    if (vpcId && options.some(item => item.id === vpcId)) return;
    const defaults = options.filter(item => item.isDefault);
    const next = defaults.length ? defaults.sort((left, right) => left.id.localeCompare(right.id))[0].id : [...options].sort((left, right) => left.id.localeCompare(right.id))[0]?.id || '';
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
    if (!options) return;
    if (keyPairId && options.some(item => item.id === keyPairId)) return;
    setKeyPairId(options[0]?.id || '');
  }, [keyPairs.data?.items, keyPairId]);

  const minimumSystemDiskGib = Math.max(20, Math.ceil(selectedImage?.sizeGib || 20));
  useEffect(() => {
    if (disk < minimumSystemDiskGib) setDisk(minimumSystemDiskGib);
  }, [disk, minimumSystemDiskGib]);

  const effectiveVpcId = networkMode === 'catalog' ? vpcId : manualVpcId.trim();
  const effectiveSubnetId = networkMode === 'catalog' ? subnetId : manualSubnetId.trim();
  const effectiveSecurityGroups = networkMode === 'catalog'
    ? securityGroupIds
    : parseIds(manualSecurityGroups);
  const effectiveKeyPairId = networkMode === 'catalog' ? keyPairId : manualKeyPairId.trim();
  const spec = useMemo<CloudPurchaseSpec | null>(() => {
    if (!selectedType || !selectedImage || !region || !zone || !effectiveVpcId || !effectiveSubnetId || !effectiveSecurityGroups.length || !effectiveKeyPairId) return null;
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
  const sshCredentialsReady = Boolean(sshUsername.trim()) && (sshAuthMethod === 'password' ? Boolean(sshPassword) : Boolean(sshPrivateKey.trim()));
  const purchaseCredentials = {
    username: sshUsername.trim(),
    port: 22,
    authMethod: sshAuthMethod,
    ...(sshAuthMethod === 'password' ? { password: sshPassword } : { privateKey: sshPrivateKey,  }),
    rememberCredentials: rememberSshCredentials,
  };
  const hasRecommendedGroup = securityGroupItems.some(item => item.recommended);
  const applyRecommendedDefaults = () => {
    setName(DEFAULT_INSTANCE_NAME);
    setNetworkMode(networkCatalogSupported ? 'catalog' : 'manual');
    setSuppressTypeDefault(false);
    setDisk(Math.max(DEFAULT_SYSTEM_DISK_GIB, minimumSystemDiskGib));
    setPublicIp(publicIpSupported);
    setBandwidth(DEFAULT_PUBLIC_BANDWIDTH_MBPS);
    if (regions.data?.items?.length) setRegion(regions.data.items.find(item => item.available !== false)?.id || regions.data.items[0].id);
    if (zones.data?.items?.length) setZone(zones.data.items.find(item => item.available !== false)?.id || zones.data.items[0].id);
    const type = (items as CloudInstanceType[]).find(item => item.available !== false && item.attributes?.purchaseCompatible !== false);
    if (kind === 'instance-type' && type) { setSelectedType(type); setDefaultTypeId(type.id); }
    const image = (items as CloudImage[]).find(item => item.available !== false);
    if (kind === 'image' && image) { setSelectedImage(image); setDefaultImageId(image.id); }
  };
  const openAdvisor = () => {
    setSelectedType(null);
    setDefaultTypeId('');
    setSelectedImage(null);
    setStep('instance');
    setAdvisorOpen(true);
  };
  const closeAdvisor = () => {
    setSelectedType(null);
    setDefaultTypeId('');
    setSuppressTypeDefault(true);
    setSelectedImage(null);
    setStep('instance');
    setNetworkNotice('');
    setSelectionError('');
    setAdvisorOpen(false);
  };
  const changeZone = (next: string) => {
    setZone(next);
    setStep('instance');
    setSelectedType(null);
    setSelectedImage(null);
    setSubnetId('');
    setNetworkNotice('');
    setSelectionError('');
    networkKey.current = key();
  };
  const changeVpc = (next: string) => {
    setVpcId(next);
    setSubnetId('');
  };
  const continueWithInstance = (instance: CloudInstanceType) => {
    if (networkMutation.isPending) return;
    setSelectionError('');
    setNetworkNotice('');
    networkKey.current = key();
    if (selectionAdvisorSupported) {
      networkMutation.mutate(instance);
      return;
    }
    if (!zone) {
      setSelectionError('请先选择可用区，再继续选择兼容镜像');
      return;
    }
    setSelectedType(instance);
    setSelectedImage(null);
    setSearch('');
    setStep('image');
  };
  const continueWithImage = (image: CloudImage) => {
    setSelectedImage(image);
    setSearch('');
    setStep('configure');
  };

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
    {providerInfo && !providerInfo.credentialsConfigured && <div className="notice warning cloud-connection-notice"><AlertTriangle size={18} /><div><strong>{providerInfo.name} 尚未连接</strong><p>SDK 已安装；API 仅从服务端环境变量读取凭证。当前可查看能力和订单策略，实时目录需要配置：{providerInfo.missingEnvironment.join('、')}。</p></div>{selectionAdvisorSupported && <button type="button" className="button secondary" aria-expanded={advisorOpen} aria-controls="cloud-selection-advisor" onClick={advisorOpen ? closeAdvisor : openAdvisor}>{advisorOpen ? <><ChevronLeft size={14} />返回手动选型</> : <><Sparkles size={14} />打开选型助手</>}</button>}</div>}
    {providerInfo?.credentialsConfigured && providerInfo.message && <div className="notice warning"><AlertTriangle size={18} /><div><strong>{providerInfo.name} 购买能力受限</strong><p>{providerInfo.message}</p></div></div>}
    {providerInfo?.credentialsConfigured && <nav className="panel market-steps" aria-label="云服务器选购步骤">
      <button type="button" className={step === 'instance' ? 'active' : ''} onClick={() => setStep('instance')}><span>1</span><Cpu size={15} />选择机型</button>
      <button type="button" className={step === 'image' ? 'active' : ''} disabled={!selectedType} onClick={() => selectedType && setStep('image')}><span>2</span><ImageIcon size={15} />选择镜像</button>
      <button type="button" className={step === 'configure' ? 'active' : ''} disabled={!selectedType || !selectedImage} onClick={() => selectedType && selectedImage && setStep('configure')}><span>3</span><Settings2 size={15} />配置与购买</button>
    </nav>}
    {providerInfo?.credentialsConfigured && <section className="panel market-toolbar">
      {step === 'instance' ? <>
        <div className="field compact"><label htmlFor="market-region">地域</label><select id="market-region" value={region} onChange={event => setRegion(event.target.value)}><option value="">选择地域</option>{regions.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></div>
        <div className="field compact"><label htmlFor="market-zone">可用区（可选）</label><select id="market-zone" value={zone} onChange={event => changeZone(event.target.value)} disabled={!region}><option value="">自动选择可售可用区</option>{zones.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></div>
        {!advisorOpen && <><div className="field compact numeric-filter"><label htmlFor="min-cpu">最低 vCPU</label><input id="min-cpu" type="number" min={0} value={minCpu} onChange={event => setMinCpu(Number(event.target.value))} /></div><div className="field compact numeric-filter"><label htmlFor="min-memory">最低内存 GiB</label><input id="min-memory" type="number" min={0} step={0.5} value={minMemory} onChange={event => setMinMemory(Number(event.target.value))} /></div><label className="search-field market-search"><Search size={16} /><span className="sr-only">搜索机型</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索机型名称或 ID" /></label></>}
        {selectionAdvisorSupported && <button type="button" className="button secondary advisor-toolbar-button" aria-expanded={advisorOpen} aria-controls="cloud-selection-advisor" onClick={advisorOpen ? closeAdvisor : openAdvisor}>{advisorOpen ? <><ChevronLeft size={14} />返回手动选型</> : <><Sparkles size={14} />打开选型助手</>}</button>}
      </> : <>
        <button type="button" className="button secondary" onClick={() => setStep(step === 'image' ? 'instance' : 'image')}><ChevronLeft size={14} />返回修改{step === 'image' ? '机型' : '镜像'}</button>
        <div className="market-step-summary"><strong>{selectedType?.id}</strong><span>{zone || '尚未选择可用区'}{selectedImage ? ` · ${selectedImage.name}` : ''}</span></div>
        {step === 'image' && <label className="search-field market-search"><Search size={16} /><span className="sr-only">搜索兼容镜像</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索兼容镜像名称或 ID" /></label>}
      </>}
    </section>}
    {selectionError && <div className="notice danger"><AlertTriangle size={18} /><div><strong>无法继续选购</strong><p>{selectionError}</p></div></div>}
    {networkMutation.isPending && <div className="notice"><RefreshCw className="spin" size={18} /><div><strong>正在准备网络</strong><p>正在核对可售可用区，并复用或创建可购买的子网。</p></div></div>}
    {networkNotice && step !== 'instance' && <div className="notice"><CheckCircle2 size={18} /><div><strong>网络已准备</strong><p>{networkNotice}</p></div></div>}
    {selectionAdvisorSupported && advisorOpen && step === 'instance' && <div id="cloud-selection-advisor"><CloudSelectionAdvisor key={provider} provider={provider} catalogAvailable={Boolean(providerInfo?.credentialsConfigured)} regions={regions.data?.items || []} zones={zones.data?.items || []} region={region} zone={zone} onRegionChange={setRegion} onZoneChange={changeZone} selected={selectedType} onSelect={value => value ? continueWithInstance(value) : setSelectedType(null)} /></div>}
    {providerInfo?.credentialsConfigured && step !== 'configure' && !(selectionAdvisorSupported && advisorOpen && step === 'instance') && (catalog.isLoading ? <LoadingState /> : catalog.isError ? <ErrorState error={catalog.error} onRetry={() => catalog.refetch()} /> : items.length ? <section className="panel cloud-results"><div className="panel-heading"><div><h2>{providerLabels[provider]} · {kindLabels[kind]}</h2><p>{catalogResult?.source === 'stale-cache' ? `${catalogResult.warning} · 已显示 ${displayedCatalogCount} / ${catalogResult.total}` : `已显示 ${displayedCatalogCount} / ${catalogResult?.total || 0} 个结果`}</p></div><span className="cache-state">{catalogResult?.source === 'live' ? '实时' : '缓存'}</span></div>{step === 'instance' ? <InstanceTypeTable items={items as CloudInstanceType[]} selected={selectedType} busy={networkMutation.isPending} onSelect={continueWithInstance} /> : <ImageTable items={items as CloudImage[]} selected={selectedImage} onSelect={continueWithImage} />}{catalog.hasNextPage && <button type="button" className="button secondary catalog-load-more" disabled={catalog.isFetchingNextPage} onClick={() => catalog.fetchNextPage()}>{catalog.isFetchingNextPage ? '加载中…' : `加载更多（已显示 ${displayedCatalogCount} / ${catalogResult?.total || 0}）`}</button>}</section> : <EmptyState title={step === 'image' ? '没有与所选机型兼容的镜像' : '没有匹配的云资源'} />)}

    {step === 'configure' && <section className="panel launch-panel">
      <div className="panel-heading"><div><h2>购买草稿</h2><p>仅按量付费；点击购买后，服务端会自动重验价格、库存和金额上限。</p></div><ShieldCheck size={20} /></div>
      <div className={`quick-create-banner ${spec ? 'ready' : ''}`}>
        <span className="quick-create-icon"><Sparkles size={17} /></span>
        <div><strong>{spec ? '推荐配置已就绪，可以直接询价' : '推荐配置正在自动填充'}</strong><p>默认 1 台 · 按量付费 · {DEFAULT_SYSTEM_DISK_GIB} GiB 系统盘 · 自动申请公网 IP（{DEFAULT_PUBLIC_BANDWIDTH_MBPS} Mbps），购买后便于平台直接 SSH 接入。</p></div>
        <button type="button" className="button secondary compact-button" onClick={applyRecommendedDefaults}><RefreshCw size={13} />恢复推荐</button>
      </div>
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
          <label><span>私有网络 VPC *</span><select id="launch-vpc" value={vpcId} disabled={!networkQueriesEnabled || !region || vpcs.isLoading} onChange={event => changeVpc(event.target.value)}><option value="">{!operatorAccessReady ? '需要操作员认证' : vpcs.isLoading ? '正在读取 VPC…' : '选择 VPC'}</option>{vpcs.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}{item.isDefault ? ' · 默认' : ''}{item.cidrBlock ? ` · ${item.cidrBlock}` : ''}</option>)}</select></label>
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
          <label><span>SSH 密钥 *</span><select id="launch-key-pair" value={keyPairId} disabled={!networkQueriesEnabled || !region || keyPairs.isLoading} onChange={event => setKeyPairId(event.target.value)}><option value="">{keyPairs.isLoading ? '正在读取 SSH 密钥…' : keyPairs.data?.items.length ? '选择 SSH 密钥' : '未找到 SSH 密钥'}</option>{keyPairs.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select><small>{keyPairs.data?.items.length ? '已自动选择第一把密钥；购买后请在订单详情配置对应私钥。' : '当前地域没有云端密钥，不能购买无法被 Looper 接入的机器。请先在云厂商控制台创建密钥后刷新目录。'}</small></label>
          {catalogError && <div className="network-catalog-error full"><AlertTriangle size={16} /><span>云网络目录读取失败。</span><button type="button" onClick={() => setNetworkMode('manual')}>改用手动 ID</button></div>}
          {managedGroupMutation.isError && <div className="inline-error full">{managedGroupMutation.error instanceof Error ? managedGroupMutation.error.message : '安全组创建失败'}</div>}
        </> : <>
          <label><span>VPC ID *</span><input value={manualVpcId} onChange={event => setManualVpcId(event.target.value)} placeholder="vpc-..." /></label>
          <label><span>子网 / vSwitch ID *</span><input value={manualSubnetId} onChange={event => setManualSubnetId(event.target.value)} placeholder="subnet-..." /></label>
          <label><span>安全组 ID *</span><input value={manualSecurityGroups} onChange={event => setManualSecurityGroups(event.target.value)} placeholder="最多 5 个，用逗号分隔" /></label>
          <label><span>SSH 密钥 ID *</span><input required value={manualKeyPairId} onChange={event => setManualKeyPairId(event.target.value)} placeholder="云厂商中已存在的密钥 ID" /><small>必须使用已导入云厂商的公钥；购买后在订单详情提供对应私钥。</small></label>
        </>}

        <div className="ssh-credentials-panel full">
          <div className="ssh-credentials-heading"><div><strong>购买后自动接入 SSH</strong><small>平台会用这组凭据读取机器参数、部署 Worker；是否保存到本机加密凭据仓库由下方开关决定。密码和私钥不会写入订单数据库。</small></div><LockKeyhole size={17} /></div>
          <div className="form-grid ssh-credentials-grid">
            <label><span>SSH 用户名 *</span><input value={sshUsername} onChange={event => setSshUsername(event.target.value)} placeholder="root 或 ubuntu" /></label>
            <label><span>SSH 端口 *</span><input value="22" readOnly /></label>
            <label><span>认证方式 *</span><select value={sshAuthMethod} onChange={event => setSshAuthMethod(event.target.value as PurchaseAuthMethod)}><option value="private-key">SSH 私钥</option><option value="password">SSH 密码</option></select></label>
            {sshAuthMethod === 'password' ? <label><span>SSH 密码 *</span><input type="password" value={sshPassword} onChange={event => setSshPassword(event.target.value)} autoComplete="new-password" /></label> : <label className="full"><span>SSH 私钥文件 *</span><div className="ssh-key-file-picker"><input aria-label="SSH 私钥文件 *" type="file" accept=".pem,.key,.pub,application/x-pem-file,text/plain" onChange={async event => { const file = event.target.files?.[0]; if (!file) return; const reader = new FileReader(); reader.onload = () => setSshPrivateKey(String(reader.result || '')); reader.readAsText(file); setSshKeyFileName(file.name); }} /><span>{sshKeyFileName || '请选择 .pem 或 .key 文件'}</span><Upload size={15} /></div><small>平台只读取文件内容用于本次 SSH 连接，不会上传原始文件路径。</small></label>}
            <label className="checkbox-field ssh-save-field full"><input type="checkbox" checked={rememberSshCredentials} onChange={event => setRememberSshCredentials(event.target.checked)} /><span>购买后保存密钥 / 密码</span><small>{rememberSshCredentials ? '保存到本机加密凭据仓库，后续可自动测试和恢复 Worker。' : '仅本次购买使用，成功后不会保存。'}</small></label>
          </div>
        </div>
        <label><span>系统盘 GB</span><input type="number" min={minimumSystemDiskGib} max={2048} value={disk} onChange={event => setDisk(Math.max(minimumSystemDiskGib, Number(event.target.value)))} /><small>所选镜像至少需要 {minimumSystemDiskGib} GiB</small></label>
        <label className="checkbox-field"><input type="checkbox" checked={publicIp} disabled={!publicIpSupported} onChange={event => setPublicIp(event.target.checked)} /><span>{publicIpSupported ? '分配固定带宽公网 IP' : '公网 IP 需独立定价流程'}</span><small>{publicIp ? '推荐保留，平台购买后才能直接 SSH 接入。' : '关闭后需要确保 Looper 能访问该实例私网地址。'}</small></label>
        <label><span>公网带宽 Mbps</span><input type="number" min={0} max={1000} disabled={!publicIp} value={bandwidth} onChange={event => setBandwidth(Number(event.target.value))} /></label>
      </div>
      <div className="launch-summary"><div><span>已选机型</span><strong>{selectedType ? `${selectedType.id} · ${selectedType.cpu} vCPU / ${selectedType.memoryGib} GiB${defaultTypeId === selectedType.id ? ' · 推荐' : ''}` : '未选择'}</strong></div><div><span>已选镜像</span><strong>{selectedImage ? `${selectedImage.name}${defaultImageId === selectedImage.id ? ' · 推荐' : ''}` : '未选择'}</strong></div><button className="button primary" disabled={!spec || !quoteSupported || quoteMutation.isPending || !operatorAccessReady} onClick={() => spec && quoteMutation.mutate({ spec, key: quoteKey.current, signature: specSignature })}><Calculator size={16} />{!operatorAccessReady ? '需要操作员认证' : !quoteSupported ? '报价配置未完成' : quoteMutation.isPending ? '询价中...' : '获取小时报价'}</button></div>
      {quoteMutation.isError && <div className="inline-error">{quoteMutation.error instanceof Error ? quoteMutation.error.message : '询价失败'}</div>}
      {quote && quoteMatchesCurrentSpec && <div className="quote-card"><div><span>报价快照</span><strong>{quote.hourlyAmount} {quote.currency}<small> / 小时{quote.estimated ? ' · 预计' : ''}</small></strong><em>{providerLabels[quote.provider]} · {quote.spec.region} · {quote.spec.instanceType} · {quote.spec.imageId} · {quote.spec.count} 台</em><em>有效至 {new Date(quote.expiresAt).toLocaleString()}</em></div><button className="button primary" disabled={purchaseMutation.isPending || quote.estimated || !quoteMatchesCurrentSpec || !purchaseReady || !sshCredentialsReady} onClick={() => quoteMatchesCurrentSpec && purchaseReady && sshCredentialsReady && purchaseMutation.mutate({ quoteId: quote.id, credentials: purchaseCredentials })}><ShoppingCart size={16} />{quote.estimated ? '估算价不可购买' : !purchaseReady ? '购买门禁未就绪' : !sshCredentialsReady ? '请先填写 SSH 凭据' : purchaseMutation.isPending ? '正在购买...' : '立即购买'}</button></div>}
      {purchaseMutation.isError && <div className="inline-error">{purchaseMutation.error instanceof Error ? purchaseMutation.error.message : '购买失败'}</div>}
    </section>}
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

function InstanceTypeTable({ items, selected, busy, onSelect }: { items: CloudInstanceType[]; selected: CloudInstanceType | null; busy: boolean; onSelect: (value: CloudInstanceType) => void }) {
  return <div className="table-wrap cloud-instance-table"><table><thead><tr><th>机型</th><th>规格</th><th>架构</th><th>库存提示</th><th /></tr></thead><tbody>{items.map(item => { const purchaseCompatible = item.attributes?.purchaseCompatible !== false; const blockedReason = typeof item.attributes?.purchaseBlockReason === 'string' ? item.attributes.purchaseBlockReason : ''; return <tr key={item.id} className={selected?.id === item.id ? 'selected-row' : ''}><td className="instance-primary"><strong>{item.id}</strong><span className="cell-meta">{item.family || '通用型'}</span>{blockedReason && <span className="cell-meta">{blockedReason}</span>}</td><td className="instance-detail"><span className="instance-mobile-label" aria-hidden="true">规格</span>{item.cpu} vCPU · {item.memoryGib} GiB</td><td className="instance-detail"><span className="instance-mobile-label" aria-hidden="true">架构</span>{item.architecture || '—'}</td><td className="instance-detail"><span className="instance-mobile-label" aria-hidden="true">库存</span><span className={`stock-label ${item.available === true && purchaseCompatible ? 'available' : item.available === false ? 'unavailable' : 'unknown'}`}>{!purchaseCompatible ? '不兼容 VPC' : item.available === true ? '可用' : item.available === false ? '不足' : '未知'}</span></td><td className="instance-action"><button className="button secondary compact-button" disabled={busy || item.available === false || !purchaseCompatible} onClick={() => onSelect(item)}>{!purchaseCompatible ? '不可购买' : item.available === false ? '不可用' : busy ? '准备中…' : '选择并继续'}</button></td></tr>; })}</tbody></table></div>;
}

function ImageTable({ items, selected, onSelect }: { items: CloudImage[]; selected: CloudImage | null; onSelect: (value: CloudImage) => void }) {
  return <div className="table-wrap cloud-image-table"><table><thead><tr><th>镜像</th><th>平台</th><th>架构</th><th>大小</th><th /></tr></thead><tbody>{items.map(item => <tr key={item.id} className={selected?.id === item.id ? 'selected-row' : ''}><td className="image-primary"><strong>{item.name}</strong><span className="cell-meta">{item.id}</span></td><td className="image-detail" data-mobile-label="平台"><span className="image-mobile-label" aria-hidden="true">平台</span>{item.platform || '—'}</td><td className="image-detail" data-mobile-label="架构"><span className="image-mobile-label" aria-hidden="true">架构</span>{item.architecture || '—'}</td><td className="image-detail" data-mobile-label="大小"><span className="image-mobile-label" aria-hidden="true">大小</span>{item.sizeGib ? `${item.sizeGib} GiB` : '—'}</td><td className="image-action"><button className="button secondary compact-button" disabled={item.available === false} onClick={() => onSelect(item)}>{item.available === false ? '不可用' : '选择并继续'}</button></td></tr>)}</tbody></table></div>;
}
