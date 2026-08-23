import type {
  AnalysisData, Benchmark, BenchmarkRegistration, BenchmarkRegistrationDraft, CloudCatalogResponse, CloudImage, CloudInstanceType, CloudOrder,
  CloudOrderEvent, CloudOrderEvidence, CloudProviderId, CloudProviderInfo, CloudPurchaseReadiness, CloudPurchaseSpec,
  CloudKeyPair, CloudQuote, CloudReconciliationContext, CloudRegion, CloudSecurityGroup, CloudSubnet, CloudVpc, CloudZone,
  DashboardData, Experiment, GlobalSearchResult, ListResponse, PostOptimizationStatus, SelectionAdvisorRequest,
  SelectionAdvisorResponse, SourceDiscovery, SourceDiscoveryProviderConfig, SourceDiscoveryReadiness, Target, VariabilityData,
} from './types';

export const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000/api/v1').replace(/\/$/, '');

const OPERATOR_TOKEN_KEY = 'looper.operator-token';
export const OPERATOR_AUTH_INVALID_EVENT = 'looper:operator-auth-invalid';

export function getOperatorToken() {
  try { return window.sessionStorage.getItem(OPERATOR_TOKEN_KEY) || ''; } catch { return ''; }
}

export function setOperatorToken(value: string) {
  try {
    if (value) window.sessionStorage.setItem(OPERATOR_TOKEN_KEY, value);
    else window.sessionStorage.removeItem(OPERATOR_TOKEN_KEY);
  } catch { /* Session storage can be unavailable in hardened browsers. */ }
}

export class ApiError extends Error {
  constructor(message: string, public status: number, public body?: unknown) { super(message); }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const operatorToken = getOperatorToken();
  const isFormData = typeof FormData !== 'undefined' && init?.body instanceof FormData;
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      ...(!isFormData ? { 'Content-Type': 'application/json' } : {}),
      Accept: 'application/json',
      ...(operatorToken ? { Authorization: `Bearer ${operatorToken}` } : {}),
      ...init?.headers,
    },
  });
  const body = response.status === 204 ? undefined : await response.json().catch(() => undefined);
  if (!response.ok) {
    const message = body && typeof body === 'object' && 'message' in body
      ? String(body.message)
      : body && typeof body === 'object' && 'detail' in body
        ? (typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail))
        : `请求失败 (${response.status})`;
    const operatorAuthRequired = response.status === 401
      && body && typeof body === 'object' && 'code' in body
      && body.code === 'operator_auth_required';
    if (operatorAuthRequired) {
      if (operatorToken) setOperatorToken('');
      window.dispatchEvent(new CustomEvent(OPERATOR_AUTH_INVALID_EVENT, { detail: { message } }));
    }
    throw new ApiError(message, response.status, body);
  }
  return body as T;
}

function list<T>(value: T[] | ListResponse<T> | { data?: T[] }): ListResponse<T> {
  if (Array.isArray(value)) return { items: value, total: value.length };
  if ('items' in value && Array.isArray(value.items)) return value;
  const items = 'data' in value && Array.isArray(value.data) ? value.data : [];
  return { items, total: items.length };
}

function query(values: Record<string, string | number | undefined>) {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => { if (value !== undefined && value !== '') params.set(key, String(value)); });
  const encoded = params.toString();
  return encoded ? `?${encoded}` : '';
}

export const api = {
  sourceDiscoveryReadiness: () => request<SourceDiscoveryReadiness>('/source-discoveries/readiness'),
  sourceDiscoveryProviderConfig: () => request<SourceDiscoveryProviderConfig>('/source-discoveries/provider-config'),
  updateSourceDiscoveryProviderConfig: (apiKey: string) => request<SourceDiscoveryProviderConfig>(
    '/source-discoveries/provider-config', { method: 'PUT', body: JSON.stringify({ apiKey }) },
  ),
  deleteSourceDiscoveryProviderConfig: () => request<SourceDiscoveryProviderConfig>(
    '/source-discoveries/provider-config', { method: 'DELETE' },
  ),
  sourceDiscoveries: async () => list(await request<SourceDiscovery[] | ListResponse<SourceDiscovery>>('/source-discoveries')),
  discoverSource: (archive: File) => {
    const body = new FormData(); body.append('archive', archive);
    return request<SourceDiscovery>('/source-discoveries', { method: 'POST', body });
  },
  dashboard: () => request<DashboardData>('/dashboard'),
  experiments: async (query = '') => list(await request<Experiment[] | ListResponse<Experiment> | { data?: Experiment[] }>(`/experiments${query}`)),
  experiment: (id: string) => request<Experiment>(`/experiments/${encodeURIComponent(id)}`),
  deleteExperiment: (id: string) => request<void>(`/experiments/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  analysis: (id: string) => request<AnalysisData>(`/experiments/${encodeURIComponent(id)}/analysis`),
  postOptimization: (id: string) => request<PostOptimizationStatus>(`/experiments/${encodeURIComponent(id)}/post-optimization`),
  startPostOptimization: (id: string) => request<PostOptimizationStatus>(`/experiments/${encodeURIComponent(id)}/post-optimization`, { method: 'POST' }),
  variability: (id: string) => request<VariabilityData>(`/experiments/${encodeURIComponent(id)}/variability`),
  benchmarks: async () => list(await request<Benchmark[] | ListResponse<Benchmark> | { data?: Benchmark[] }>('/benchmarks')),
  benchmarkRegistration: (id: string) => request<BenchmarkRegistration>(`/benchmark-registrations/${encodeURIComponent(id)}`),
  createBenchmarkRegistration: (draft: BenchmarkRegistrationDraft) => request<BenchmarkRegistration>(
    '/benchmark-registrations', { method: 'POST', body: JSON.stringify(draft) },
  ),
  importBenchmarkRegistration: (configuration: File) => {
    const body = new FormData(); body.append('configuration', configuration);
    return request<BenchmarkRegistration>('/benchmark-registrations/import', { method: 'POST', body });
  },
  updateBenchmarkRegistration: (id: string, expectedRevision: number, draft: BenchmarkRegistrationDraft) =>
    request<BenchmarkRegistration>(`/benchmark-registrations/${encodeURIComponent(id)}`, {
      method: 'PUT', body: JSON.stringify({ expectedRevision, draft }),
    }),
  registerBenchmark: (id: string, expectedRevision: number) => request<BenchmarkRegistration>(
    `/benchmark-registrations/${encodeURIComponent(id)}/register`, {
      method: 'POST', body: JSON.stringify({ expectedRevision }),
    },
  ),
  createBenchmarkSmokeRun: (
    benchmarkId: string, version: string, payload: { targetId?: string; workloadId?: string; parameters?: Record<string, unknown>; inputBindings?: Record<string, unknown> } = {},
  ) => request<Experiment>(
    `/benchmarks/${encodeURIComponent(benchmarkId)}/versions/${encodeURIComponent(version)}/smoke-runs`,
    { method: 'POST', body: JSON.stringify(payload) },
  ),
  targets: async (includeInactive = true) => {
    const response = list(await request<Target[] | ListResponse<Target> | { data?: Target[] }>('/targets'));
    return includeInactive ? response : {
      ...response,
      items: response.items.filter(item => item.lifecycleStatus !== 'missing' && item.lifecycleStatus !== 'archived'),
    };
  },
  syncTencentTargets: (region = 'ap-guangzhou') => request<ListResponse<Target>>(
    `/targets/tencent-cvm/sync?region=${encodeURIComponent(region)}`, { method: 'POST' },
  ),
  importExternalTarget: (payload: Record<string, unknown>) => request<Target>(
    '/targets/import', { method: 'POST', body: JSON.stringify(payload) },
  ),
  connectExternalTarget: (payload: Record<string, unknown>) => request<Target>(
    '/targets/connect', { method: 'POST', body: JSON.stringify(payload) },
  ),
  createExperiment: (payload: Record<string, unknown>) => request<Experiment>('/experiments', { method: 'POST', body: JSON.stringify(payload) }),
  experimentAction: (id: string, action: 'start' | 'pause' | 'resume' | 'cancel') => request<Experiment>(`/experiments/${encodeURIComponent(id)}/${action}`, { method: 'POST' }),
  retryAttempt: (id: string) => request<unknown>(`/attempts/${encodeURIComponent(id)}/retry`, { method: 'POST' }),
  operatorSession: () => request<{ required: boolean; configured: boolean; authenticated: boolean; operatorGateReady: boolean }>('/operator/session'),
  cloudAuthStatus: () => api.operatorSession(),
  purchaseReadiness: () => request<CloudPurchaseReadiness>('/cloud/purchase-readiness'),
  providers: async () => list(await request<CloudProviderInfo[] | ListResponse<CloudProviderInfo>>('/cloud/providers')),
  catalog: <T>(provider: CloudProviderId, kind: string, params: Record<string, string | number | undefined> = {}) =>
    request<CloudCatalogResponse<T>>(`/cloud/catalog/${provider}/${kind}${query(params)}`),
  regions: (provider: CloudProviderId) => api.catalog<CloudRegion>(provider, 'region'),
  zones: (provider: CloudProviderId, region: string) => api.catalog<CloudZone>(provider, 'zone', { region }),
  instanceTypes: (provider: CloudProviderId, params: Record<string, string | number | undefined>) =>
    api.catalog<CloudInstanceType>(provider, 'instance-type', params),
  images: (provider: CloudProviderId, params: Record<string, string | number | undefined>) =>
    api.catalog<CloudImage>(provider, 'image', params),
  selectionAdvisor: (payload: SelectionAdvisorRequest) => request<SelectionAdvisorResponse>(
    '/cloud/selection-advisor/search', { method: 'POST', body: JSON.stringify(payload) },
  ),
  vpcs: (provider: CloudProviderId, region: string) => api.catalog<CloudVpc>(provider, 'vpc', { region }),
  subnets: (provider: CloudProviderId, region: string, zone: string, vpcId: string) =>
    api.catalog<CloudSubnet>(provider, 'subnet', { region, zone, vpc_id: vpcId }),
  securityGroups: (provider: CloudProviderId, region: string) =>
    api.catalog<CloudSecurityGroup>(provider, 'security-group', { region }),
  keyPairs: (provider: CloudProviderId, region: string) => api.catalog<CloudKeyPair>(provider, 'key-pair', { region }),
  ensureManagedSecurityGroup: (provider: CloudProviderId, region: string) =>
    request<CloudSecurityGroup>(`/cloud/network/${provider}/managed-security-group${query({ region })}`, { method: 'POST' }),
  quoteById: (id: string) => request<CloudQuote>(`/cloud/quotes/${encodeURIComponent(id)}`),
  quote: (spec: CloudPurchaseSpec, key: string) => request<CloudQuote>('/cloud/quotes', {
    method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ spec }),
  }),
  prepareOrder: (quoteId: string, key: string) => request<CloudOrder>('/cloud/orders/prepare', {
    method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ quoteId }),
  }),
  renewOrderConfirmation: (id: string) => request<CloudOrder>(
    `/cloud/orders/${encodeURIComponent(id)}/renew-confirmation`,
    { method: 'POST' },
  ),
  orders: async (status = '') => list(await request<CloudOrder[] | ListResponse<CloudOrder>>(`/cloud/orders${status ? `?status=${encodeURIComponent(status)}` : ''}`)),
  order: (id: string) => request<CloudOrder>(`/cloud/orders/${encodeURIComponent(id)}`),
  orderEvents: async (id: string) => list(await request<CloudOrderEvent[] | ListResponse<CloudOrderEvent>>(`/cloud/orders/${encodeURIComponent(id)}/events`)),
  orderReconciliationContext: (id: string) => request<CloudReconciliationContext>(`/cloud/orders/${encodeURIComponent(id)}/reconciliation-context`),
  orderEvidence: (id: string) => request<CloudOrderEvidence>(`/cloud/orders/${encodeURIComponent(id)}/evidence`),
  confirmOrder: (id: string, payload: { confirmationToken: string; acknowledgement: string; expectedHourlyAmount: string }) =>
    request<CloudOrder>(`/cloud/orders/${encodeURIComponent(id)}/confirm`, { method: 'POST', body: JSON.stringify(payload) }),
  resolveOrder: (id: string, payload: { resolution: 'submitted' | 'not_created'; instanceIds: string[]; providerOrderId?: string; note: string }) =>
    request<CloudOrder>(`/cloud/orders/${encodeURIComponent(id)}/resolve`, { method: 'POST', body: JSON.stringify(payload) }),
  searchAll: (value: string) => request<{ items: GlobalSearchResult[]; total: number; query: string }>(`/search?q=${encodeURIComponent(value)}`),
};
