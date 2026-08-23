export type ExperimentStatus = 'draft' | 'queued' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';

export interface SourceDiscoveryReadiness { configured: boolean; provider: string; model: string; baseUrl: string; maxArchiveBytes: number; acceptedMediaTypes: string[]; requiredEnvironment: string[]; dataDisclosure: string }
export interface InterfaceEvidence { file: string; startLine: number; endLine: number; excerptDigest: string }
export interface DiscoveredInterface { id: string; protocol: string; method: string; path: string; summary: string; handler: { symbol?: string | null }; parameters: Array<{ name: string; in: 'path' | 'query' | 'header' | 'cookie'; required: boolean; schema: Record<string, unknown> }>; requestBody?: { required: boolean; contentTypes: string[]; schema: Record<string, unknown> } | null; responses: Array<{ statusCode: string; contentTypes: string[]; schema: Record<string, unknown> }>; authentication: string[]; sideEffect: string; confidence: number; evidence: InterfaceEvidence[]; unresolved: string[] }
export interface InterfaceContract { apiVersion: 'looper.dev/interface-contract/v1'; kind: 'InterfaceContract'; metadata: { provider: string; model: string; harnessVersion: string; sourceDigest: string }; spec: { interfaces: DiscoveredInterface[]; unresolved: string[] } }
export interface SourceDiscovery { id: string; archiveName: string; sourceDigest: string; status: 'running' | 'completed' | 'failed'; provider: string; model: string; fileManifest: Array<{ path: string; bytes: number; sha256: string }>; excludedFiles: Array<{ path: string; reason: string }>; contract?: InterfaceContract | null; trace: Array<Record<string, unknown>>; error?: { code: string; message: string } | null; createdAt: string; completedAt?: string | null }

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
export interface PostOptimizationAction {
  id: string; label: string; description?: string; risk: 'low' | 'medium' | 'high'; applyMode: 'benchmark-parameter';
  parameter: string; value: unknown; before?: unknown; after?: unknown; minimumImprovementRatio: number;
  guardMetric?: string; maximumGuardRegressionRatio?: number;
}
export interface PostOptimizationStatus {
  eligible: boolean;
  status: 'ready' | 'retesting' | 'accepted' | 'rolled_back' | 'inconclusive' | 'unavailable' | 'failed';
  reason: string; action?: PostOptimizationAction; baselineParameters?: Record<string, unknown>;
  candidateParameters?: Record<string, unknown>; followUpExperiment?: Experiment;
}
export interface DashboardData { counts?: Partial<Record<ExperimentStatus, number>>; activeExperiments?: Experiment[]; recentExperiments?: Experiment[]; trend?: Array<{ time: string; score: number; baseline?: number }>; successRate?: number; totalExperiments?: number; computeHours?: number }
export interface Benchmark {
  id: string; key?: string; name: string; description?: string; category?: string; version?: string; metrics?: string[];
  executionModel?: BenchmarkExecutionModel;
  inputs?: BenchmarkInputDeclaration[];
  executionPolicy?: Record<string, unknown>;
  cases?: number; updatedAt?: string; tags?: string[]; license?: string; runnable?: boolean; executionStatus?: string;
  decisionQuestion?: string; primaryMetric?: string; scenario?: ScenarioContract;
  selectionReady?: boolean;
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
export interface Target {
  id: string; name: string; type?: string; endpoint?: string;
  status?: 'online' | 'offline' | 'degraded' | 'unknown' | 'inventory';
  lifecycleStatus?: 'active' | 'missing' | 'archived'; framework?: string; version?: string;
  hardware?: string; lastSeenAt?: string; lastInventorySeenAt?: string; missingSince?: string;
  inventoryMissCount?: number; archivedAt?: string; archiveReason?: string;
  tags?: string[]; runnable?: boolean;
  credentialsRemembered?: boolean;
  deployment?: { status: string; workerId: string; remotePid: number; transport?: string; restartSafe?: boolean; deployedAt: string };
  fingerprint?: {
    processor?: string; logical_cpu_count?: number; memory_gib?: number; instance_type?: string;
    system?: string; release?: string; architecture?: string; host_key_sha256?: string; host_key_type?: string;
  };
}
export interface AnalysisData {
  mode?: 'optimization' | 'selection'; targets?: SelectionTargetResult[]; comparisons?: SelectionComparison[];
  pareto?: Array<{ id?: string; candidate: string; score: number; cost: number; latency?: number; rank?: number | null; feasible?: boolean; objectives?: Record<string, number> }>;
  evidence?: Array<{ id: string; title: string; kind?: string; summary?: string; createdAt?: string; artifacts?: Artifact[] }>;
}
export interface ListResponse<T> { items: T[]; total?: number }

export type VariabilityStatus = 'stable' | 'warning' | 'unstable' | 'insufficient_evidence';
export interface VariabilityDistribution {
  count: number; mean: number; median: number; standardDeviation: number; coefficientOfVariation?: number | null;
  minimum: number; maximum: number; p05?: number | null; p95?: number | null; p99?: number | null;
  iqr?: number | null; mad?: number | null; tailMean?: number | null; skewness?: number | null;
}
export interface VariabilityGroupReport {
  groupLabel: string; metric: string; unit: string; direction: string; status: VariabilityStatus;
  distribution: VariabilityDistribution;
  stability: { verdict: VariabilityStatus; cv?: number | null; slow_run_share?: number; skewed?: boolean; suspected_multimodal?: boolean; reasons: string[] };
  modes?: { cutoff: number; fastMode: { count: number; center: number }; slowMode: { count: number; center: number } } | null;
  runs: Array<{ runId: string; value: number; label: string; slow: boolean }>;
  outliers: { slow: string[]; fast: string[] };
  associationClues: Array<{ metric: string; correlation: number; lift?: number | null; direction: string; slowMean?: number | null; normalMean?: number | null; likelyConsequence: boolean; note: string }>;
  attribution: Array<{ dimension: string; etaSquared?: number | null; groupCount: number; dominant: boolean; groupMeans?: Record<string, number> }>;
  recommendations: Array<{ action: string; rationale: string; priority: 'high' | 'medium' | 'low'; kind: string }>;
  selectionImpact: { summary: string; confidence: string; details?: string[] };
  evidence: { sampleCount: number; hostCount?: number; distinctDates?: number; systemMetricCount?: number };
}
export interface VariabilityComparison {
  metric: string; unit: string; direction: string; baselineLabel: string; candidateLabel: string;
  baseline: VariabilityDistribution; candidate: VariabilityDistribution;
  meanImprovement?: number | null; medianImprovement?: number | null; tailImprovement?: number | null; cvRatio?: number | null;
  slowRunProbability: { baseline?: number | null; candidate?: number | null };
  worstHost: { baseline?: { host: string; median: number } | null; candidate?: { host: string; median: number } | null };
  sloExceedance?: { threshold: number; baseline_exceedance: number; candidate_exceedance: number };
  tailWorsened: boolean; verdict: string; summary: string; recommendation: string;
}
export interface VariabilityData {
  experiment_id: string; mode?: string; metric: string; unit: string; direction: string;
  status: string; group_statuses?: string[]; groups: VariabilityGroupReport[]; comparisons: VariabilityComparison[];
  policy?: Record<string, unknown>; input_digest?: string; policy_digest?: string;
  evidence?: { attempt_count?: number; run_group_count?: number; system_metric_names?: string[] };
}

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
  gpu?: number; gpuModel?: string; gpuMemoryGib?: number; architecture?: string;
  networkBandwidthRxGbps?: number; networkBandwidthTxGbps?: number; networkPpsRx?: number; networkPpsTx?: number;
  localStorageCount?: number; localStorageCapacityGib?: number; localStorageCategory?: string;
  zones: string[]; available?: boolean; attributes?: Record<string, unknown>;
}
export type SelectionScenario = 'web-api' | 'microservices-rpc' | 'database' | 'cache' | 'search-logs' |
  'big-data-messaging' | 'game' | 'video' | 'ai' | 'development-test' | 'other';
export interface SelectionAdvisorRequest {
  provider: 'alibaba' | 'tencent'; region: string; zone?: string; primaryScenario: SelectionScenario;
  coLocatedComponents: SelectionScenario[]; sizingMode: 'exact' | 'unknown'; exactCpu?: number;
  exactMemoryGib?: number; workloadScale?: string; minimumGpuCount: number;
  localStorage: 'required' | 'not-required' | 'unknown'; minimumNetworkBandwidthGbps?: number;
  minimumNetworkPps?: number; codeAvailability: 'available' | 'unavailable' | 'unknown';
  architecture: 'x86' | 'arm' | 'unknown'; offset: number; limit: number;
}
export interface SelectionExclusionStage {
  code: string; label: string; before: number; after: number; removed: number;
}
export interface AdvisedCloudInstanceType extends CloudInstanceType {
  matchTier: 'preferred' | 'suitable' | 'other'; reasons: string[]; warnings: string[];
}
export interface SelectionAdvisorResponse {
  provider: 'alibaba' | 'tencent'; region: string; zone?: string; items: AdvisedCloudInstanceType[]; total: number;
  offset: number; limit: number; nextOffset?: number; exclusionStages: SelectionExclusionStage[];
  mostRestrictiveStage?: SelectionExclusionStage; source: 'live' | 'cache' | 'stale-cache';
  fetchedAt: string; expiresAt: string; stale: boolean; warning?: string;
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
