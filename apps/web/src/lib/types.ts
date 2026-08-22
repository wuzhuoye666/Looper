export type ExperimentStatus = 'draft' | 'queued' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';

export interface Artifact { name: string; url: string; type?: string }
export interface Metric { name: string; value: number; unit?: string; baseline?: number; direction?: 'min' | 'max' }
export interface Evaluation { id: string; attemptId?: string; candidate: string; status: ExperimentStatus; score?: number; duration?: number; cost?: number; createdAt?: string; metrics?: Metric[]; artifacts?: Artifact[]; error?: string }
export interface ScenarioContract {
  id: string; name: string; decision_question: string; user_value: string; workload_class: string;
  topology: 'single-node' | 'client-server' | 'multi-node' | 'closed-loop'; primary_metric: string;
  roles?: Array<{ id: string; kind: string; count?: number; included_in_score?: boolean; description?: string }>;
  tail_evidence?: { minimum_samples: number; required_statistics: string[]; histogram_format: string };
  load_search?: { type: string; boundary_repeats: number; resolution_ratio: number; minimum_effect_ratio: number };
}
export interface SelectionComparison {
  metric: string; unit: string; baseline_variant: string; candidate_variant: string; inference_unit?: string;
  placement_pair_count: number; estimate?: number; lower?: number; upper?: number; minimum_effect_ratio?: number;
  distinguishable: boolean; winner?: string; status: string; reason?: string; conclusion_strength: string;
}
export interface SelectionTargetResult {
  target_id: string; variant_id: string; label: string; placement_pair_id: string; status: string;
  attempt_count: number; valid_block_count: number; invalid_block_count: number;
  price?: { hourly_amount: string; currency: string; quote_digest: string };
  price_efficiency?: { value: number; unit: string; price_snapshot_digest: string };
  metrics: Array<{ metric: string; unit: string; direction: string; raw?: number; block_count: number; status: string }>;
}
export interface Experiment {
  id: string; name: string; description?: string; status: ExperimentStatus; mode?: 'optimization' | 'selection';
  targetId?: string; targetName?: string; targetIds?: string[]; targetNames?: string[];
  benchmarkId?: string; benchmarkName?: string; progress?: number; bestScore?: number; baselineScore?: number;
  createdAt?: string; updatedAt?: string; owner?: string; attempts?: number; maxAttempts?: number;
  objective?: string; decisionQuestion?: string; scenario?: ScenarioContract; comparison?: SelectionComparison;
  config?: Record<string, unknown>; evaluations?: Evaluation[]; artifacts?: Artifact[];
}
export interface DashboardData { counts?: Partial<Record<ExperimentStatus, number>>; activeExperiments?: Experiment[]; recentExperiments?: Experiment[]; trend?: Array<{ time: string; score: number; baseline?: number }>; successRate?: number; totalExperiments?: number; computeHours?: number }
export interface Benchmark {
  id: string; key?: string; name: string; description?: string; category?: string; version?: string; metrics?: string[];
  executionModel?: BenchmarkExecutionModel;
  inputs?: BenchmarkInputDeclaration[];
  executionPolicy?: Record<string, unknown>;
  cases?: number; updatedAt?: string; tags?: string[]; license?: string; runnable?: boolean; executionStatus?: string;
  decisionQuestion?: string; primaryMetric?: string; scenario?: ScenarioContract;
  registrationId?: string; registrationStatus?: string;
  auditStatus?: 'legacy-unreviewed' | 'registered-not-admitted';
}
export interface BenchmarkInputDeclaration {
  id: string; kind: 'dataset' | 'artifact' | 'config' | 'endpoint' | 'secret' | 'device' | 'topology';
  required: boolean; mediaType?: string; mountPath?: string; digestRequired?: boolean; description?: string;
}
export type BenchmarkRuntimeType = 'container' | 'local-process' | 'benchexec';
export type BenchmarkExecutionStatus = 'stage0-adapter-only' | 'executable';
export type BenchmarkExecutionModel = 'batch-suite' | 'service-stack' | 'database' | 'storage' | 'network' | 'distributed' | 'accelerator' | 'custom';
export interface BenchmarkRegistrationDraft {
  name: string; benchmarkId: string; version: string; sourceUrl: string; sourceRevision: string;
  license: string; category: string; executionModel: BenchmarkExecutionModel; decisionQuestion: string; primaryMetric: string; primaryUnit: string;
  correctnessContract: string; runtimeType: BenchmarkRuntimeType; executionStatus: BenchmarkExecutionStatus;
  image: string; minimumSamples: number; repeats: number; hasReference: boolean;
  retainsRawEvidence: boolean; crossEnvironmentAudit: boolean; manifest?: Record<string, unknown>;
}
export interface BenchmarkRegistrationConstraint {
  code: string; group: string; label: string; status: 'pass' | 'fail'; blocking: boolean; detail: string;
}
export interface BenchmarkRegistration {
  id: string; status: 'draft' | 'registered'; revision: number; draft: BenchmarkRegistrationDraft;
  constraints: BenchmarkRegistrationConstraint[]; readyToRegister: boolean; manifestDigest?: string;
  benchmarkKey?: string; createdAt: string; updatedAt: string; registeredAt?: string;
}
export interface Target { id: string; name: string; type?: string; endpoint?: string; status?: 'online' | 'offline' | 'degraded' | 'unknown' | 'inventory'; framework?: string; version?: string; hardware?: string; lastSeenAt?: string; tags?: string[]; runnable?: boolean }
export interface AnalysisData {
  mode?: 'optimization' | 'selection'; targets?: SelectionTargetResult[]; comparisons?: SelectionComparison[];
  pareto?: Array<{ id?: string; candidate: string; score: number; cost: number; latency?: number }>;
  evidence?: Array<{ id: string; title: string; kind?: string; summary?: string; createdAt?: string; artifacts?: Artifact[] }>;
}
export interface ListResponse<T> { items: T[]; total?: number }

export type CloudProviderId = 'tencent' | 'alibaba' | 'volcengine' | 'baidu';
export interface CloudReadinessCheck { code: string; label: string; ready: boolean; detail: string }
export interface CloudProviderReadiness {
  provider: CloudProviderId; name: string; ready: boolean; checks: CloudReadinessCheck[]; missingEnvironment: string[];
}
export interface CloudPurchaseReadiness {
  livePurchaseEnabled: boolean; operatorTokenReady: boolean; confirmationSecretReady: boolean;
  maxHourlyAmount: string; providers: CloudProviderReadiness[];
}
export interface CloudProviderInfo {
  id: CloudProviderId; name: string; sdkPackage: string; sdkInstalled: boolean;
  credentialsConfigured: boolean; missingEnvironment: string[]; capabilities: string[];
  livePurchaseEnabled: boolean; message?: string;
}
export interface CloudRegion { provider: CloudProviderId; id: string; name: string; endpoint?: string; available?: boolean }
export interface CloudZone { provider: CloudProviderId; region: string; id: string; name: string; available?: boolean }
export interface CloudVpc {
  provider: CloudProviderId; region: string; id: string; name: string; cidrBlock?: string; isDefault: boolean;
}
export interface CloudSubnet {
  provider: CloudProviderId; region: string; zone: string; vpcId: string; id: string; name: string;
  cidrBlock?: string; availableIpCount?: number; isDefault: boolean;
}
export interface CloudSecurityGroup {
  provider: CloudProviderId; region: string; id: string; name: string; description?: string;
  isDefault: boolean; recommended: boolean; tags: Record<string, string>;
}
export interface CloudKeyPair {
  provider: CloudProviderId; region: string; id: string; name: string; description?: string;
  createdAt?: string; associatedInstanceCount: number;
}
export interface CloudInstanceType {
  provider: CloudProviderId; region: string; id: string; family?: string; cpu: number; memoryGib: number;
  gpu?: number; architecture?: string; zones: string[]; available?: boolean; attributes?: Record<string, unknown>;
}
export interface CloudImage {
  provider: CloudProviderId; region: string; id: string; name: string; platform?: string; architecture?: string;
  imageType?: string; sizeGib?: number; createdAt?: string; available?: boolean; attributes?: Record<string, unknown>;
}
export interface CloudCatalogResponse<T> {
  provider: CloudProviderId; resourceType: string; items: T[]; total: number;
  source: 'live' | 'cache' | 'stale-cache'; fetchedAt: string; expiresAt: string; stale: boolean; warning?: string;
}
export interface CloudPurchaseSpec {
  provider: CloudProviderId; region: string; zone: string; instanceType: string; cpu?: number; memoryGib?: number;
  imageId: string; instanceName: string; count: number; billingMode: 'postpaid'; vpcId: string; subnetId: string;
  securityGroupIds: string[]; keyPairId?: string; systemDiskType?: string; systemDiskGib: number;
  publicIp: boolean; internetBandwidthMbps: number; tags: Record<string, string>;
}
export interface CloudQuote {
  id: string; provider: CloudProviderId; status: string; spec: CloudPurchaseSpec; specDigest: string;
  providerQuoteId?: string; hourlyAmount: string; currency: string; estimated: boolean; quoteDigest: string;
  details: Record<string, unknown>; expiresAt: string; createdAt: string;
}
export interface CloudOrder {
  id: string; quoteId: string; provider: CloudProviderId; status: string; spec: CloudPurchaseSpec;
  specDigest: string; quoteDigest: string; hourlyAmount: string; currency: string; providerOrderId?: string;
  instanceIds: string[]; providerResponse: Record<string, unknown>; errorCode?: string; errorMessage?: string;
  confirmationExpiresAt: string; confirmationToken?: string; acknowledgement?: string;
  createdAt: string; updatedAt: string; confirmedAt?: string; submittedAt?: string;
}
export interface CloudOrderEvent {
  id: string; sequence: number; eventType: string; entityType: string; entityId: string;
  payload: Record<string, unknown>; createdAt: string;
}
export interface CloudReconciliationContext {
  orderId: string; provider: CloudProviderId; status: 'unknown'; clientToken: string;
  providerOrderId?: string; providerRequestId?: string; instanceIds: string[]; instanceName?: string;
  region?: string; submittedAt?: string; createdAt: string;
}
export interface CloudOrderEvidence {
  schemaVersion: string; generatedAt: string; quote: CloudQuote;
  order: CloudOrder & { idempotencyKey: string; clientTokenDigest: string; confirmationPhraseHash: string };
  events: Array<CloudOrderEvent & { idempotencyKey: string }>; evidenceDigest: string;
}

export interface GlobalSearchResult {
  type: 'experiment' | 'benchmark' | 'target' | 'quote' | 'order'; id: string; title: string;
  subtitle?: string; status?: string; url: string; updatedAt?: string; metadata?: Record<string, unknown>;
}
