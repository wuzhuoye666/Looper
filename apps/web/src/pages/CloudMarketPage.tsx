import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  Calculator,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  Cloud,
  Cpu,
  LockKeyhole,
  Network,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  ShieldCheck,
  ShoppingCart,
  Terminal,
  XCircle,
} from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { useLocation, useNavigate, useSearchParams } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { InstanceTypeFacetFilter } from '../components/InstanceTypeFacetFilter';
import { InstancePricePreview } from '../components/InstancePricePreview';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';
import type {
  CloudCatalogResponse,
  CloudImage,
  CloudInstanceType,
  CloudProviderId,
  CloudProviderReadiness,
  CloudPurchaseSpec,
  CloudQuote,
  CloudSecurityGroup,
  CloudSubnet,
  CloudVpc,
  InstanceSelectionClass,
} from '../lib/types';

const providerLabels: Record<CloudProviderId, string> = {
  tencent: '腾讯云 CVM',
  alibaba: '阿里云 ECS',
  volcengine: '火山引擎 ECS',
  baidu: '百度智能云 BCC',
};
type CatalogKind = 'instance-type' | 'image';
type NetworkMode = 'catalog' | 'manual';
type MarketStep = 'instance' | 'configure';
type SshAuthMethod = 'password' | 'private-key';
const CATALOG_PAGE_SIZE = 20;
const IMAGE_CATALOG_LIMIT = 500;
const DEFAULT_INSTANCE_NAME = 'looper-instance';
const DEFAULT_SYSTEM_DISK_GIB = 50;
const DEFAULT_PUBLIC_BANDWIDTH_MBPS = 1;
const MAX_SYSTEM_DISK_GIB = 2048;
const MAX_PUBLIC_BANDWIDTH_MBPS = 200;
const COMMON_IMAGE_FAMILIES = ['Ubuntu', 'Debian', 'CentOS', 'Alibaba Cloud Linux', 'TencentOS', 'Windows'] as const;
type CommonImageFamily = typeof COMMON_IMAGE_FAMILIES[number];
const COMMON_IMAGE_VERSION_PREFERENCES: Record<CommonImageFamily, RegExp[]> = {
  Ubuntu: [/^ubuntu(?:\s+server)?\s+22\.04\b/i, /^ubuntu(?:\s+server)?\s+24\.04\b/i, /^ubuntu(?:\s+server)?\s+20\.04\b/i],
  Debian: [/^debian\s+12(?:\.|\s|$)/i, /^debian\s+11(?:\.|\s|$)/i, /^debian\s+10(?:\.|\s|$)/i],
  CentOS: [/^centos(?:\s+stream)?\s+9(?:\.|\s|$)/i, /^centos(?:\s+stream)?\s+8(?:\.|\s|$)/i, /^centos\s+7(?:\.|\s|$)/i],
  'Alibaba Cloud Linux': [/^(?:alibaba\s+cloud\s+linux|alinux)\s+3(?:\.|\s|$)/i, /^(?:alibaba\s+cloud\s+linux|alinux)\s+2(?:\.|\s|$)/i],
  TencentOS: [/^tencentos(?:\s+server)?\s+4(?:\.|\s|$)/i, /^tencentos(?:\s+server)?\s+3(?:\.|\s|$)/i],
  Windows: [/^windows(?:\s+server)?\s+2022\b/i, /^windows(?:\s+server)?\s+2019\b/i, /^windows(?:\s+server)?\s+2016\b/i],
};

function key() {
  return `looper-${Date.now()}-${window.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)}`;
}

function mergeCatalogItem<T extends { id: string }>(
  catalog: CloudCatalogResponse<T> | undefined,
  item: T,
) {
  if (!catalog) return catalog;
  const items = [item, ...catalog.items.filter(candidate => candidate.id !== item.id)];
  return { ...catalog, items, total: Math.max(catalog.total, items.length) };
}

function parseIds(value: string) {
  return [...new Set(value.split(',').map(item => item.trim()).filter(Boolean))].slice(0, 5);
}

function defaultVpcId(items: CloudVpc[] | undefined) {
  return [...(items || [])].sort((left, right) =>
    Number(right.isDefault) - Number(left.isDefault) || left.id.localeCompare(right.id))[0]?.id || '';
}

function defaultSubnetId(items: CloudSubnet[] | undefined) {
  const options = items || [];
  const defaults = options.filter(item => item.isDefault).sort((left, right) => left.id.localeCompare(right.id));
  return defaults[0]?.id || (options.length === 1 ? options[0].id : '');
}

function defaultSecurityGroupIds(items: CloudSecurityGroup[] | undefined, vpcId: string) {
  const compatible = (items || []).filter(item => !item.vpcId || !vpcId || item.vpcId === vpcId);
  const preferred = compatible.find(item => item.recommended)
    || compatible.find(item => item.isDefault)
    || (compatible.length === 1 ? compatible[0] : undefined);
  return preferred ? [preferred.id] : [];
}

function validCloudPassword(value: string) {
  const specialCharacters = "()`~!@#$%^&*-+=_|{}[]:;'<>.,?/";
  const categories = [/[a-z]/.test(value), /[A-Z]/.test(value), /[0-9]/.test(value),
    [...value].some(character => specialCharacters.includes(character))].filter(Boolean).length;
  return value.length >= 8 && value.length <= 30 && categories >= 3 && !value.startsWith('/')
    && [...value].every(character => /[A-Za-z0-9]/.test(character) || specialCharacters.includes(character));
}

function validatedNumber(value: string, minimum: number, maximum: number, fallback: number, integer = false) {
  const parsed = Number(value.trim());
  if (!value.trim() || !Number.isFinite(parsed)) return fallback;
  const normalized = integer ? Math.trunc(parsed) : parsed;
  return Math.min(maximum, Math.max(minimum, normalized));
}

function commonImageFamily(image: CloudImage) {
  const label = image.name;
  if (/ubuntu/i.test(label)) return 'Ubuntu';
  if (/debian/i.test(label)) return 'Debian';
  if (/\bcentos\b/i.test(label)) return 'CentOS';
  if (/alibaba\s*cloud\s*linux|\balinux\b/i.test(label)) return 'Alibaba Cloud Linux';
  if (/tencentos/i.test(label)) return 'TencentOS';
  if (/windows/i.test(label)) return 'Windows';
  return null;
}

function commonImageScore(image: CloudImage, family: CommonImageFamily) {
  const name = image.name.trim();
  const officialName = /^(ubuntu(?:\s+server)?|debian|centos|alibaba\s+cloud\s+linux|alinux|tencentos|windows(?:\s+server)?)(?:\s|$)/i.test(name);
  const preferredVersion = COMMON_IMAGE_VERSION_PREFERENCES[family].findIndex(pattern => pattern.test(name));
  return (officialName ? 1_000 : 0) + (preferredVersion >= 0 ? 100 - preferredVersion : 0) + (image.imageType === 'PUBLIC_IMAGE' ? 1 : 0);
}

function prioritizedCommonImages(images: CloudImage[]) {
  return COMMON_IMAGE_FAMILIES.flatMap(family => {
    const candidates = images
      .filter(image => commonImageFamily(image) === family && image.available !== false)
      .sort((left, right) => commonImageScore(right, family) - commonImageScore(left, family));
    if (family === 'Ubuntu') {
      const preferred = COMMON_IMAGE_VERSION_PREFERENCES.Ubuntu
        .slice(0, 2)
        .map(pattern => candidates.find(image => pattern.test(image.name)))
        .filter((image): image is CloudImage => Boolean(image));
      for (const candidate of candidates) {
        if (preferred.length >= 2) break;
        if (!preferred.some(image => image.id === candidate.id)) preferred.push(candidate);
      }
      return preferred;
    }
    return candidates.slice(0, 1);
  });
}

function imageLabel(image: CloudImage) {
  return `${image.name} · ${image.id}${image.sizeGib ? ` · ${image.sizeGib} GiB` : ''}`;
}

function marketProvider(value: string | null): CloudProviderId | null {
  return value && ['tencent', 'alibaba', 'volcengine', 'baidu'].includes(value)
    ? value as CloudProviderId
    : null;
}

export function CloudMarketPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const providers = useQuery({ queryKey: ['cloud-providers'], queryFn: api.providers, staleTime: 30_000 });
  const readiness = useQuery({ queryKey: ['cloud-purchase-readiness'], queryFn: api.purchaseReadiness, staleTime: 15_000 });
  const auth = useQuery({ queryKey: ['cloud-auth-status'], queryFn: api.cloudAuthStatus, staleTime: 15_000 });
  const available = providers.data?.items || [];
  const routeState = location.state as { preselectedInstance?: CloudInstanceType } | null;
  const preselectedInstance = routeState?.preselectedInstance;
  const requestedProvider = marketProvider(searchParams.get('provider')) || preselectedInstance?.provider || 'tencent';
  const requestedRegion = searchParams.get('region') || preselectedInstance?.region || '';
  const requestedZone = searchParams.get('zone') || preselectedInstance?.zones?.[0] || '';
  const requestedInstanceType = searchParams.get('instanceType') || preselectedInstance?.id || '';
  const initialMarketParams = useRef({
    provider: requestedProvider,
    region: requestedRegion,
    zone: requestedZone,
    instanceType: requestedInstanceType,
  });
  const providerReset = useRef(false);
  const regionReset = useRef(false);
  const requestedInstanceApplied = useRef(false);
  const [provider, setProvider] = useState<CloudProviderId>(requestedProvider);
  const [region, setRegion] = useState(requestedRegion);
  const [zone, setZone] = useState(requestedZone);
  const [step, setStep] = useState<MarketStep>('instance');
  const [search, setSearch] = useState(requestedInstanceType);
  const [catalogSearch, setCatalogSearch] = useState(requestedInstanceType);
  const [minCpu, setMinCpu] = useState(1);
  const [minMemory, setMinMemory] = useState(1);
  const [minCpuDraft, setMinCpuDraft] = useState('1');
  const [minMemoryDraft, setMinMemoryDraft] = useState('1');
  const [architectureClass, setArchitectureClass] = useState<InstanceSelectionClass | undefined>();
  const [typeKind, setTypeKind] = useState<string | undefined>();
  const [familyToken, setFamilyToken] = useState<string | undefined>();
  const [selectedType, setSelectedType] = useState<CloudInstanceType | null>(null);
  const [selectedImage, setSelectedImage] = useState<CloudImage | null>(null);
  const [showAdditionalImages, setShowAdditionalImages] = useState(false);
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
  const [sshAuthMethod, setSshAuthMethod] = useState<SshAuthMethod>('password');
  const [sshPassword, setSshPassword] = useState('');
  const [disk, setDisk] = useState(DEFAULT_SYSTEM_DISK_GIB);
  const [diskDraft, setDiskDraft] = useState(String(DEFAULT_SYSTEM_DISK_GIB));
  const [publicIp, setPublicIp] = useState(true);
  const [bandwidth, setBandwidth] = useState(DEFAULT_PUBLIC_BANDWIDTH_MBPS);
  const [bandwidthDraft, setBandwidthDraft] = useState(String(DEFAULT_PUBLIC_BANDWIDTH_MBPS));
  const [rememberSshCredentials, setRememberSshCredentials] = useState(true);
  const [quote, setQuote] = useState<CloudQuote | null>(null);
  const [quoteSignature, setQuoteSignature] = useState('');
  const [networkNotice, setNetworkNotice] = useState('');
  const [selectionError, setSelectionError] = useState('');
  const currentSpecSignature = useRef('');
  const quoteKey = useRef(key());
  const orderKey = useRef(key());
  const networkKey = useRef(key());
  const sshPasswordInitialized = useRef(false);
  const kind: CatalogKind = step === 'instance' ? 'instance-type' : 'image';

  const providerInfo = available.find(item => item.id === provider);
  const automaticNetworkSetupSupported = provider === 'alibaba' || provider === 'tencent';
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
  const managedSecurityGroupSupported = Boolean(providerInfo?.capabilities.includes('managed-security-group'));

  const sshDefaults = useQuery({
    queryKey: ['cloud-ssh-defaults'],
    queryFn: api.cloudSshDefaults,
    enabled: operatorAccessReady,
    staleTime: 300_000,
  });

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
    queryKey: ['cloud-catalog', provider, kind, region, zone, selectedType?.id, catalogSearch, minCpu, minMemory, architectureClass, typeKind, familyToken],
    queryFn: ({ pageParam }) => api.catalog<CloudInstanceType | CloudImage>(provider, kind, {
      region,
      zone: kind === 'instance-type' ? zone : undefined,
      instance_type: kind === 'image' ? selectedType?.id : undefined,
      query: catalogSearch,
      min_cpu: kind === 'instance-type' && minCpu ? minCpu : undefined,
      min_memory_gib: kind === 'instance-type' && minMemory ? minMemory : undefined,
      architecture_class: kind === 'instance-type' ? architectureClass : undefined,
      type_kind: kind === 'instance-type' ? typeKind : undefined,
      family_token: kind === 'instance-type' ? familyToken : undefined,
      offset: pageParam,
      limit: kind === 'image' ? IMAGE_CATALOG_LIMIT : CATALOG_PAGE_SIZE,
    }),
    initialPageParam: 0,
    getNextPageParam: lastPage => lastPage.nextOffset ?? undefined,
    enabled: !!region && !!providerInfo?.credentialsConfigured && (step === 'instance' || !!selectedType),
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
    enabled: networkQueriesEnabled && sshAuthMethod === 'private-key' && !!region && !!providerInfo?.capabilities.includes('key-pairs'),
    staleTime: 30_000,
  });
  const catalogPages = catalog.data?.pages || [];
  const catalogResult = catalogPages[0];
  const items = catalogPages.flatMap(page => page.items);
  const displayedCatalogCount = items.length;
  const securityGroupItems = useMemo(
    () => [...(securityGroups.data?.items || [])]
      .filter(item => provider !== 'alibaba' || !item.vpcId || item.vpcId === vpcId)
      .sort((left, right) =>
        Number(right.recommended) - Number(left.recommended) || left.name.localeCompare(right.name)),
    [provider, securityGroups.data?.items, vpcId],
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
    mutationFn: (request: { quoteId: string; rememberCredentials: boolean }) => api.purchaseQuote(request.quoteId, orderKey.current, {
      sshAuthMethod,
      sshPassword: sshAuthMethod === 'password' ? sshPassword : undefined,
      rememberCredentials: request.rememberCredentials,
    }),
    onSuccess: order => {
      void queryClient.invalidateQueries({ queryKey: ['targets'] });
      navigate(`/cloud/orders/${order.id}`, { state: order });
    },
  });
  const managedGroupMutation = useMutation({
    mutationFn: () => api.ensureManagedSecurityGroup(provider, region, vpcId || undefined),
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
      queryClient.setQueryData<CloudCatalogResponse<CloudVpc>>(
        ['cloud-vpcs', provider, region],
        current => mergeCatalogItem(current, resolution.vpc),
      );
      queryClient.setQueryData<CloudCatalogResponse<CloudSubnet>>(
        ['cloud-subnets', provider, region, resolution.zone, resolution.vpc.id],
        current => mergeCatalogItem(current, resolution.subnet),
      );
      if (resolution.securityGroup) {
        queryClient.setQueryData<CloudCatalogResponse<CloudSecurityGroup>>(
          ['cloud-security-groups', provider, region],
          current => mergeCatalogItem(current, resolution.securityGroup!),
        );
        setSecurityGroupIds([resolution.securityGroup.id]);
      } else {
        setSecurityGroupIds([]);
      }
      setSelectedType(instance);
      setSelectedImage(null);
      setZone(resolution.zone);
      setVpcId(resolution.vpc.id);
      setSubnetId(resolution.subnet.id);
      const groupNotice = resolution.securityGroup
        ? `${resolution.securityGroupAction === 'created' ? '已创建' : '已复用'}安全组 ${resolution.securityGroup.name} · ${resolution.securityGroup.id}`
        : '存在多个安全组，请在配置页手动选择';
      setNetworkNotice(`${resolution.zoneAutomaticallySelected ? `已选择可售可用区 ${resolution.zone}` : `可用区 ${resolution.zone}`}；${resolution.vpcAction === 'created' ? '已创建' : '已复用'} VPC ${resolution.vpc.name} · ${resolution.vpc.id}；${resolution.subnetAction === 'created' ? '已创建' : '已复用'}子网 ${resolution.subnet.name} · ${resolution.subnet.id}；${groupNotice}`);
      setSelectionError('');
      setSearch('');
      setCatalogSearch('');
      setStep('configure');
      void queryClient.invalidateQueries({ queryKey: ['cloud-vpcs', provider, region] });
      void queryClient.invalidateQueries({ queryKey: ['cloud-subnets', provider, region] });
    },
    onError: error => setSelectionError(error instanceof Error ? error.message : '网络准备失败'),
  });

  const resetTransientErrors = () => {
    setSelectionError('');
    managedGroupMutation.reset();
    networkMutation.reset();
    quoteMutation.reset();
    purchaseMutation.reset();
  };

  useEffect(() => {
    if (!sshDefaults.data || sshPasswordInitialized.current) return;
    setSshAuthMethod('password');
    setSshPassword(sshDefaults.data.password || '');
    sshPasswordInitialized.current = true;
  }, [sshDefaults.data]);
  useEffect(() => {
    if (step !== 'configure') return;
    resetTransientErrors();
    void Promise.all([
      queryClient.invalidateQueries({ queryKey: ['cloud-vpcs', provider, region] }),
      queryClient.invalidateQueries({ queryKey: ['cloud-subnets', provider, region] }),
      queryClient.invalidateQueries({ queryKey: ['cloud-security-groups', provider, region] }),
      ...(sshAuthMethod === 'private-key'
        ? [queryClient.invalidateQueries({ queryKey: ['cloud-key-pairs', provider, region] })]
        : []),
    ]);
  }, [step, provider, region, sshAuthMethod]);
  useEffect(() => {
    setStep('instance');
    const isInitialRoute = !providerReset.current && provider === initialMarketParams.current.provider;
    providerReset.current = true;
    if (isInitialRoute) {
      setRegion(initialMarketParams.current.region);
      setZone(initialMarketParams.current.zone);
      setSearch(initialMarketParams.current.instanceType);
      setCatalogSearch(initialMarketParams.current.instanceType);
    } else {
      setRegion('');
      setZone('');
      setSelectedType(null);
      setSearch('');
      setCatalogSearch('');
    }
    setSelectedImage(null);
    setDefaultTypeId('');
    setDefaultImageId('');
    setSuppressTypeDefault(false);
    setArchitectureClass(undefined);
    setTypeKind(undefined);
    setFamilyToken(undefined);
    setNetworkMode(provider === 'tencent' || provider === 'alibaba' ? 'catalog' : 'manual');
    setSshAuthMethod('password');
    if (sshDefaults.data) setSshPassword(sshDefaults.data.password || '');
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
    resetTransientErrors();
  }, [provider, publicIpSupported]);
  useEffect(() => {
    setStep('instance');
    const isInitialRoute = !regionReset.current && region === initialMarketParams.current.region;
    regionReset.current = true;
    if (isInitialRoute) {
      setZone(initialMarketParams.current.zone);
      setSearch(initialMarketParams.current.instanceType);
      setCatalogSearch(initialMarketParams.current.instanceType);
    } else {
      setZone('');
      setSelectedType(null);
      setSearch('');
      setCatalogSearch('');
    }
    setSelectedImage(null);
    setDefaultTypeId('');
    setDefaultImageId('');
    setSuppressTypeDefault(false);
    setArchitectureClass(undefined);
    setTypeKind(undefined);
    setFamilyToken(undefined);
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
    resetTransientErrors();
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
    if (kind !== 'instance-type' || suppressTypeDefault || selectedType || !items.length) return;
    const requested = initialMarketParams.current.instanceType
      ? (items as CloudInstanceType[]).find(item => item.id === initialMarketParams.current.instanceType) || preselectedInstance
      : undefined;
    if (requested && !requestedInstanceApplied.current) {
      requestedInstanceApplied.current = true;
      setSuppressTypeDefault(true);
      continueWithInstance(requested);
      return;
    }
    const preferred = (items as CloudInstanceType[]).find(item => item.available !== false && item.attributes?.purchaseCompatible !== false);
    if (preferred) {
      setSelectedType(preferred);
      setDefaultTypeId(preferred.id);
    }
  }, [items, kind, selectedType, suppressTypeDefault]);

  useEffect(() => {
    const imageItems = kind === 'image' ? (items as CloudImage[]) : [];
    if (selectedImage || !imageItems.length) return;
    const preferred = prioritizedCommonImages(imageItems)[0] || imageItems.find(item => item.available !== false);
    if (preferred) {
      setSelectedImage(preferred);
      setDefaultImageId(preferred.id);
    }
  }, [items, kind, selectedImage]);
  useEffect(() => {
    setShowAdditionalImages(false);
  }, [provider, region, selectedType?.id]);

  useEffect(() => {
    const options = vpcs.data?.items;
    if (!options) return;
    if (vpcId && options.some(item => item.id === vpcId)) return;
    setVpcId(defaultVpcId(options));
  }, [vpcs.data?.items, vpcId]);
  useEffect(() => {
    const options = subnets.data?.items;
    if (!options) return;
    if (subnetId && options.some(item => item.id === subnetId)) return;
    setSubnetId(defaultSubnetId(options));
  }, [subnets.data?.items, subnetId]);
  useEffect(() => {
    const options = securityGroupItems;
    if (!options) return;
    const valid = securityGroupIds.filter(id => options.some(item => item.id === id));
    if (valid.length) {
      if (valid.length !== securityGroupIds.length) setSecurityGroupIds(valid);
      return;
    }
    const next = defaultSecurityGroupIds(options, vpcId);
    if (next.length || securityGroupIds.length) setSecurityGroupIds(next);
  }, [securityGroupItems, securityGroupIds, vpcId]);
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
  useEffect(() => {
    setDiskDraft(String(disk));
  }, [disk]);
  useEffect(() => {
    setBandwidthDraft(String(bandwidth));
  }, [bandwidth]);

  const commitMinCpu = () => {
    const next = validatedNumber(minCpuDraft, 0, 4096, minCpu, true);
    setMinCpu(next);
    setMinCpuDraft(String(next));
  };
  const commitMinMemory = () => {
    const next = validatedNumber(minMemoryDraft, 0, 65536, minMemory);
    setMinMemory(next);
    setMinMemoryDraft(String(next));
  };
  const commitDisk = () => {
    const next = validatedNumber(diskDraft, minimumSystemDiskGib, MAX_SYSTEM_DISK_GIB, minimumSystemDiskGib, true);
    setDisk(next);
    setDiskDraft(String(next));
  };
  const commitBandwidth = () => {
    const next = validatedNumber(bandwidthDraft, 0, MAX_PUBLIC_BANDWIDTH_MBPS, bandwidth, true);
    setBandwidth(next);
    setBandwidthDraft(String(next));
  };

  const effectiveVpcId = networkMode === 'catalog' ? vpcId : manualVpcId.trim();
  const effectiveSubnetId = networkMode === 'catalog' ? subnetId : manualSubnetId.trim();
  const effectiveSecurityGroups = networkMode === 'catalog'
    ? securityGroupIds
    : parseIds(manualSecurityGroups);
  const effectiveKeyPairId = networkMode === 'catalog' ? keyPairId : manualKeyPairId.trim();
  const spec = useMemo<CloudPurchaseSpec | null>(() => {
    if (!selectedType || !selectedImage || !region || !zone || !effectiveVpcId || !effectiveSubnetId || !effectiveSecurityGroups.length) return null;
    if (sshAuthMethod === 'private-key' && !effectiveKeyPairId) return null;
    if (sshAuthMethod === 'password' && !validCloudPassword(sshPassword)) return null;
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
      keyPairId: sshAuthMethod === 'private-key' ? effectiveKeyPairId : undefined,
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
    sshAuthMethod,
    sshPassword,
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
  const imageItems = kind === 'image' ? (items as CloudImage[]) : [];
  const commonImages = prioritizedCommonImages(imageItems);
  const commonImageIds = new Set(commonImages.map(image => image.id));
  const additionalImages = imageItems.filter(image => !commonImageIds.has(image.id));
  const restoreConfigurationDefaults = () => {
    const nextVpcId = defaultVpcId(vpcs.data?.items);
    setName(DEFAULT_INSTANCE_NAME);
    setNetworkMode(networkCatalogSupported ? 'catalog' : 'manual');
    if (networkCatalogSupported) {
      setVpcId(nextVpcId);
      setSubnetId(nextVpcId === vpcId ? defaultSubnetId(subnets.data?.items) : '');
      setSecurityGroupIds(defaultSecurityGroupIds(securityGroups.data?.items, nextVpcId));
    } else {
      setManualVpcId('');
      setManualSubnetId('');
      setManualSecurityGroups('');
    }
    const defaultDisk = Math.max(DEFAULT_SYSTEM_DISK_GIB, minimumSystemDiskGib);
    setDisk(defaultDisk);
    setDiskDraft(String(defaultDisk));
    setPublicIp(publicIpSupported);
    setBandwidth(DEFAULT_PUBLIC_BANDWIDTH_MBPS);
    setBandwidthDraft(String(DEFAULT_PUBLIC_BANDWIDTH_MBPS));
    setSshAuthMethod('password');
    setSshPassword(sshDefaults.data?.password || '');
  };
  const goToStep = (next: MarketStep) => {
    resetTransientErrors();
    setStep(next);
  };
  const changeZone = (next: string) => {
    resetTransientErrors();
    setZone(next);
    setArchitectureClass(undefined);
    setTypeKind(undefined);
    setFamilyToken(undefined);
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
    resetTransientErrors();
    setNetworkNotice('');
    networkKey.current = key();
    if (automaticNetworkSetupSupported) {
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
    setCatalogSearch('');
    setStep('configure');
  };
  const confirmCatalogSearch = () => setCatalogSearch(search.trim());

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
    {providerInfo && !providerInfo.credentialsConfigured && <div className="notice warning cloud-connection-notice"><AlertTriangle size={18} /><div><strong>{providerInfo.name} 尚未连接</strong><p>SDK 已安装；API 仅从服务端环境变量读取凭证。当前可查看能力和订单策略，实时目录需要配置：{providerInfo.missingEnvironment.join('、')}。</p></div></div>}
    {providerInfo?.credentialsConfigured && providerInfo.message && <div className="notice warning"><AlertTriangle size={18} /><div><strong>{providerInfo.name} 购买能力受限</strong><p>{providerInfo.message}</p></div></div>}
    {providerInfo?.credentialsConfigured && <nav className="panel market-steps" aria-label="云服务器选购步骤">
      <button type="button" className={step === 'instance' ? 'active' : ''} onClick={() => goToStep('instance')}><span>1</span><Cpu size={15} />选择机型</button>
      <button type="button" className={step === 'configure' ? 'active' : ''} disabled={!selectedType} onClick={() => selectedType && goToStep('configure')}><span>2</span><Settings2 size={15} />配置与购买</button>
    </nav>}
    {providerInfo?.credentialsConfigured && <section className="panel market-toolbar">
      {step === 'instance' ? <>
        <div className="field compact"><label htmlFor="market-region">地域</label><select id="market-region" value={region} onChange={event => setRegion(event.target.value)}><option value="">选择地域</option>{regions.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></div>
        <div className="field compact"><label htmlFor="market-zone">可用区（可选）</label><select id="market-zone" value={zone} onChange={event => changeZone(event.target.value)} disabled={!region}><option value="">自动选择可售可用区</option>{zones.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></div>
        <div className="field compact numeric-filter"><label htmlFor="min-cpu">最低 vCPU</label><input id="min-cpu" type="number" min={0} step={1} value={minCpuDraft} onChange={event => setMinCpuDraft(event.target.value)} onBlur={commitMinCpu} onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }} /></div><div className="field compact numeric-filter"><label htmlFor="min-memory">最低内存 GiB</label><input id="min-memory" type="number" min={0} step={0.5} value={minMemoryDraft} onChange={event => setMinMemoryDraft(event.target.value)} onBlur={commitMinMemory} onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }} /></div><form className="search-submit-group market-search-group" onSubmit={event => { event.preventDefault(); confirmCatalogSearch(); }}><label className="search-field market-search"><Search size={16} /><span className="sr-only">搜索机型</span><input aria-label="搜索机型" value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索机型 ID、规格族或中文类型/分组" /></label><button type="submit" className="button primary search-confirm-button" disabled={search.trim() === catalogSearch}>确认</button></form>
      </> : <>
        <button type="button" className="button secondary" onClick={() => goToStep('instance')}><ChevronLeft size={14} />返回修改机型</button>
        <div className="market-step-summary"><strong>{selectedType?.id}</strong><span>{zone || '尚未选择可用区'}{selectedImage ? ` · ${selectedImage.name}` : ''}</span></div>
      </>}
    </section>}
    {providerInfo?.credentialsConfigured && step === 'instance' && <InstanceTypeFacetFilter
      facets={catalogResult?.instanceTypeFacets}
      value={{ architectureClass, typeKind, familyToken }}
      resetKey={`${provider}:${region}:${zone}:${minCpu}:${minMemory}`}
      onChange={value => {
        setArchitectureClass(value.architectureClass);
        setTypeKind(value.typeKind);
        setFamilyToken(value.familyToken);
        setSuppressTypeDefault(true);
      }}
    />}
    {selectionError && <div className="notice danger"><AlertTriangle size={18} /><div><strong>无法继续选购</strong><p>{selectionError}</p></div></div>}
    {networkMutation.isPending && <div className="notice"><RefreshCw className="spin" size={18} /><div><strong>正在准备网络</strong><p>正在核对可售可用区，并复用或创建可购买的子网。</p></div></div>}
    {networkNotice && step !== 'instance' && <div className="notice"><CheckCircle2 size={18} /><div><strong>网络已准备</strong><p>{networkNotice}</p></div></div>}
    {providerInfo?.credentialsConfigured && step === 'instance' && (catalog.isLoading ? <LoadingState /> : catalog.isError ? <ErrorState error={catalog.error} onRetry={() => catalog.refetch()} /> : items.length ? <section className="panel cloud-results"><div className="panel-heading"><div><h2>{providerLabels[provider]} · 机型</h2><p>{catalogResult?.source === 'stale-cache' ? `${catalogResult.warning} · 已显示 ${displayedCatalogCount} / ${catalogResult.total}` : `已显示 ${displayedCatalogCount} / ${catalogResult?.total || 0} 个结果`}</p></div><span className="cache-state">{catalogResult?.source === 'live' ? '实时' : '缓存'}</span></div><InstanceTypeTable items={items as CloudInstanceType[]} selected={selectedType} busy={networkMutation.isPending} onSelect={continueWithInstance} />{catalog.hasNextPage && <button type="button" className="button secondary catalog-load-more" disabled={catalog.isFetchingNextPage} onClick={() => catalog.fetchNextPage()}>{catalog.isFetchingNextPage ? '加载中…' : `加载更多（已显示 ${displayedCatalogCount} / ${catalogResult?.total || 0}）`}</button>}</section> : <EmptyState title="没有匹配的云资源" />)}

    {step === 'configure' && <section className="panel launch-panel">
      <div className="panel-heading"><div><h2>购买草稿</h2><p>仅按量付费；点击购买后，服务端会自动重验价格、库存和金额上限。</p></div><ShieldCheck size={20} /></div>
      <div className={`quick-create-banner ${spec ? 'ready' : ''}`}>
        <span className="quick-create-icon"><Settings2 size={17} /></span>
        <div><strong>{spec ? '购买配置完整，可以直接询价' : '请补全购买配置'}</strong><p>可恢复默认实例名称、VPC、子网、安全组、SSH 密码登录、{DEFAULT_SYSTEM_DISK_GIB} GiB 系统盘（镜像要求更大时采用最低容量）和 {DEFAULT_PUBLIC_BANDWIDTH_MBPS} Mbps 公网带宽；不会更改地域、机型或镜像。</p></div>
        <button type="button" className="button secondary compact-button" onClick={restoreConfigurationDefaults}><RefreshCw size={13} />恢复默认设置</button>
      </div>
      <div className="form-grid cloud-form">
        <label><span>实例名称 *</span><input value={name} onChange={event => setName(event.target.value)} /></label>
        <label><span>操作系统镜像 *</span><select aria-label="操作系统镜像" value={selectedImage?.id || ''} disabled={catalog.isLoading || !imageItems.length} onChange={event => { if (event.target.value === '__more_images__') { setShowAdditionalImages(true); return; } const image = imageItems.find(item => item.id === event.target.value); if (image) { setSelectedImage(image); setDefaultImageId(''); } }}><option value="">{catalog.isLoading ? '正在读取兼容镜像…' : imageItems.length ? '选择操作系统镜像' : '暂无兼容镜像'}</option>{commonImages.map(image => <option key={image.id} value={image.id}>{imageLabel(image)}</option>)}{!showAdditionalImages && additionalImages.length > 0 && <option value="__more_images__">更多镜像…</option>}{showAdditionalImages && additionalImages.length > 0 && <optgroup label="更多镜像">{additionalImages.map(image => <option key={image.id} value={image.id}>{imageLabel(image)}</option>)}</optgroup>}</select><small>默认显示常用系统；选择“更多镜像”后展开其余版本。</small></label>
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
            {!hasRecommendedGroup && managedSecurityGroupSupported && region && operatorAccessReady && <button type="button" className="button secondary compact-button managed-group-button" disabled={managedGroupMutation.isPending} onClick={() => managedGroupMutation.mutate()}><Plus size={14} />{managedGroupMutation.isPending ? '创建中…' : '创建 Looper 安全组'}</button>}
          </div>
          {sshAuthMethod === 'private-key' && <label><span>SSH 密钥 *</span><select id="launch-key-pair" value={keyPairId} disabled={!networkQueriesEnabled || !region || keyPairs.isLoading} onChange={event => setKeyPairId(event.target.value)}><option value="">{keyPairs.isLoading ? '正在读取 SSH 密钥…' : keyPairs.data?.items.length ? '选择 SSH 密钥' : '未找到 SSH 密钥'}</option>{keyPairs.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select><small>{keyPairs.data?.items.length ? '云厂商公钥资源；平台购买后使用本机统一私钥自动接入。' : '当前地域没有云端密钥，请先在云厂商控制台创建后刷新。'}</small></label>}
          {catalogError && <div className="network-catalog-error full"><AlertTriangle size={16} /><span>云网络目录读取失败。</span><button type="button" onClick={() => setNetworkMode('manual')}>改用手动 ID</button></div>}
          {managedGroupMutation.isError && <div className="inline-error full">{managedGroupMutation.error instanceof Error ? managedGroupMutation.error.message : '安全组创建失败'}</div>}
        </> : <>
          <label><span>VPC ID *</span><input value={manualVpcId} onChange={event => setManualVpcId(event.target.value)} placeholder="vpc-..." /></label>
          <label><span>子网 / vSwitch ID *</span><input value={manualSubnetId} onChange={event => setManualSubnetId(event.target.value)} placeholder="subnet-..." /></label>
          <label><span>安全组 ID *</span><input value={manualSecurityGroups} onChange={event => setManualSecurityGroups(event.target.value)} placeholder="最多 5 个，用逗号分隔" /></label>
          {sshAuthMethod === 'private-key' && <label><span>SSH 密钥 ID *</span><input required value={manualKeyPairId} onChange={event => setManualKeyPairId(event.target.value)} placeholder="云厂商中已存在的密钥 ID" /><small>必须使用已导入云厂商的公钥；平台购买后使用本机统一私钥自动接入。</small></label>}
        </>}

        <label><span>SSH 登录方式 *</span><select value={sshAuthMethod} onChange={event => setSshAuthMethod(event.target.value as SshAuthMethod)}><option value="password">SSH 密码</option><option value="private-key">SSH 密钥</option></select></label>
        {sshAuthMethod === 'password' && <label><span>SSH 默认密码 *</span><input aria-label="SSH 默认密码 *" type="password" value={sshPassword} onChange={event => setSshPassword(event.target.value)} autoComplete="new-password" /><small>{validCloudPassword(sshPassword) ? '可直接修改；修改值仅用于本次购买。' : '需要 8–30 位，并包含至少三类大小写字母、数字或特殊字符。'}</small></label>}
        <label className="checkbox-field ssh-save-field full"><input type="checkbox" checked={rememberSshCredentials} onChange={event => setRememberSshCredentials(event.target.checked)} /><span>购买后保存密钥 / 密码</span></label>
        <label><span>系统盘 GB</span><input aria-label="系统盘 GB" type="number" min={minimumSystemDiskGib} max={MAX_SYSTEM_DISK_GIB} value={diskDraft} onChange={event => setDiskDraft(event.target.value)} onBlur={commitDisk} onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }} /><small>所选镜像至少需要 {minimumSystemDiskGib} GiB</small></label>
        <label className="checkbox-field"><input type="checkbox" checked={publicIp} disabled={!publicIpSupported} onChange={event => setPublicIp(event.target.checked)} /><span>{publicIpSupported ? '分配固定带宽公网 IP' : '公网 IP 需独立定价流程'}</span><small>{publicIp ? '推荐保留，平台购买后才能直接 SSH 接入。' : '关闭后需要确保 Looper 能访问该实例私网地址。'}</small></label>
        <label><span>公网带宽 Mbps</span><input aria-label="公网带宽 Mbps" type="number" min={0} max={MAX_PUBLIC_BANDWIDTH_MBPS} disabled={!publicIp} value={bandwidthDraft} onChange={event => setBandwidthDraft(event.target.value)} onBlur={commitBandwidth} onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur(); }} /><small>最大支持 {MAX_PUBLIC_BANDWIDTH_MBPS} Mbps</small></label>
      </div>
      <div className="launch-summary"><div><span>已选机型</span><strong>{selectedType ? `${selectedType.id} · ${selectedType.cpu} vCPU / ${selectedType.memoryGib} GiB${defaultTypeId === selectedType.id ? ' · 推荐' : ''}` : '未选择'}</strong></div><div><span>已选镜像</span><strong>{selectedImage ? `${selectedImage.name}${defaultImageId === selectedImage.id ? ' · 推荐' : ''}` : '未选择'}</strong></div><button className="button primary" disabled={!spec || !quoteSupported || quoteMutation.isPending || !operatorAccessReady} onClick={() => spec && quoteMutation.mutate({ spec, key: quoteKey.current, signature: specSignature })}><Calculator size={16} />{!operatorAccessReady ? '需要操作员认证' : !quoteSupported ? '报价配置未完成' : quoteMutation.isPending ? '询价中...' : '获取小时报价'}</button></div>
      {quoteMutation.isError && <div className="inline-error">{quoteMutation.error instanceof Error ? quoteMutation.error.message : '询价失败'}</div>}
      {quote && quoteMatchesCurrentSpec && <div className="quote-card"><div><span>报价快照</span><strong>{quote.hourlyAmount} {quote.currency}<small> / 小时{quote.estimated ? ' · 预计' : ''}</small></strong><em>{providerLabels[quote.provider]} · {quote.spec.region} · {quote.spec.instanceType} · {quote.spec.imageId} · {quote.spec.count} 台</em><em>有效至 {new Date(quote.expiresAt).toLocaleString()}</em></div><button className="button primary" disabled={purchaseMutation.isPending || quote.estimated || !quoteMatchesCurrentSpec || !purchaseReady} onClick={() => quoteMatchesCurrentSpec && purchaseReady && purchaseMutation.mutate({ quoteId: quote.id, rememberCredentials: rememberSshCredentials })}><ShoppingCart size={16} />{quote.estimated ? '估算价不可购买' : !purchaseReady ? '购买门禁未就绪' : purchaseMutation.isPending ? '正在购买...' : '立即购买'}</button></div>}
      {purchaseMutation.isError && <div className="inline-error">{purchaseMutation.error instanceof Error ? purchaseMutation.error.message : '购买失败'}</div>}
    </section>}
  </div>;
}

function PurchaseReadiness({ provider, maxHourlyAmount, authRequired, authenticated }: { provider: CloudProviderReadiness; maxHourlyAmount: string; authRequired: boolean; authenticated: boolean }) {
  const browserCheck = { code: 'browser-auth', label: '浏览器操作员', ready: !authRequired || authenticated, detail: authRequired ? authenticated ? '当前会话已认证' : '点击顶部钥匙并输入 Operator token' : '服务器尚未要求认证' };
  const checks = [...provider.checks, browserCheck];
  const ready = provider.ready && browserCheck.ready;
  const configurable = provider.provider === 'tencent' || provider.provider === 'alibaba';
  return <details className={`purchase-readiness ${ready ? 'ready' : 'blocked'}`} aria-label={`${provider.name}购买就绪状态`}>
    <summary className="purchase-readiness-heading"><div className="readiness-title-icon">{ready ? <CheckCircle2 size={20} /> : <LockKeyhole size={20} />}</div><div><span className="eyebrow">LIVE PURCHASE</span><h2>{ready ? `${provider.name} 可以购买` : `${provider.name} 尚不可购买`}</h2><p>单笔总小时金额上限 {maxHourlyAmount} CNY</p></div><ChevronDown className="readiness-chevron" size={18} /></summary>
    <div className="readiness-grid">{checks.map(check => <div key={check.code} className={check.ready ? 'ready' : 'blocked'}>{check.ready ? <CheckCircle2 size={15} /> : <XCircle size={15} />}<span><strong>{check.label}</strong><small>{check.detail}</small></span></div>)}</div>
    {!ready && configurable && <div className="setup-command"><Terminal size={16} /><span><strong>本机配置命令</strong><code>.venv\Scripts\looper.exe cloud configure {provider.provider} --max-hourly-amount {maxHourlyAmount === '—' ? '10' : maxHourlyAmount}</code></span></div>}
  </details>;
}

function InstanceTypeTable({ items, selected, busy, onSelect }: { items: CloudInstanceType[]; selected: CloudInstanceType | null; busy: boolean; onSelect: (value: CloudInstanceType) => void }) {
  return <div className="table-wrap cloud-instance-table"><table><thead><tr><th>机型</th><th>规格</th><th>架构</th><th>库存提示</th><th>预览价格</th><th /></tr></thead><tbody>{items.map(item => { const purchaseCompatible = item.attributes?.purchaseCompatible !== false; const blockedReason = typeof item.attributes?.purchaseBlockReason === 'string' ? item.attributes.purchaseBlockReason : ''; const classification = item.typeLabel && item.familyLabel ? `${item.typeLabel} · ${item.familyLabel}` : item.family || '未标注规格族'; return <tr key={item.id} className={selected?.id === item.id ? 'selected-row' : ''}><td className="instance-primary"><strong>{item.id}</strong><span className="cell-meta">{classification}</span>{blockedReason && <span className="cell-meta">{blockedReason}</span>}</td><td className="instance-detail"><span className="instance-mobile-label" aria-hidden="true">规格</span>{item.cpu} vCPU · {item.memoryGib} GiB</td><td className="instance-detail"><span className="instance-mobile-label" aria-hidden="true">架构</span>{item.architecture || '—'}</td><td className="instance-detail"><span className="instance-mobile-label" aria-hidden="true">库存</span><span className={`stock-label ${item.available === true && purchaseCompatible ? 'available' : item.available === false ? 'unavailable' : 'unknown'}`}>{!purchaseCompatible ? '不兼容 VPC' : item.available === true ? '可用' : item.available === false ? '不足' : '未知'}</span></td><td className="instance-price-cell"><InstancePricePreview item={item} compact /></td><td className="instance-action"><button className="button secondary compact-button" disabled={busy || item.available === false || !purchaseCompatible} onClick={() => onSelect(item)}>{!purchaseCompatible ? '不可购买' : item.available === false ? '不可用' : busy ? '准备中…' : '选择并继续'}</button></td></tr>; })}</tbody></table></div>;
}

function ImageTable({ items, selected, onSelect }: { items: CloudImage[]; selected: CloudImage | null; onSelect: (value: CloudImage) => void }) {
  return <div className="table-wrap cloud-image-table"><table><thead><tr><th>镜像</th><th>平台</th><th>架构</th><th>大小</th><th /></tr></thead><tbody>{items.map(item => <tr key={item.id} className={selected?.id === item.id ? 'selected-row' : ''}><td className="image-primary"><strong>{item.name}</strong><span className="cell-meta">{item.id}</span></td><td className="image-detail" data-mobile-label="平台"><span className="image-mobile-label" aria-hidden="true">平台</span>{item.platform || '—'}</td><td className="image-detail" data-mobile-label="架构"><span className="image-mobile-label" aria-hidden="true">架构</span>{item.architecture || '—'}</td><td className="image-detail" data-mobile-label="大小"><span className="image-mobile-label" aria-hidden="true">大小</span>{item.sizeGib ? `${item.sizeGib} GiB` : '—'}</td><td className="image-action"><button className="button secondary compact-button" disabled={item.available === false} onClick={() => onSelect(item)}>{item.available === false ? '不可用' : '选择并继续'}</button></td></tr>)}</tbody></table></div>;
}
