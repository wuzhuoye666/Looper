export type ExperimentStatus = 'draft' | 'queued' | 'running' | 'paused' | 'completed' | 'failed' | 'cancelled';

export interface SourceDiscoveryReadiness { configured: boolean; provider: string; model: string; baseUrl: string; maxArchiveBytes: number; acceptedMediaTypes: string[]; requiredEnvironment: string[]; dataDisclosure: string }
export interface SourceDiscoveryProviderConfig { configured: boolean; source: 'stored' | 'environment' | null; maskedKey: string | null; provider: string; model: string; baseUrl: string; encryptedAtRest: boolean }
export interface InterfaceEvidence { file: string; startLine: number; endLine: number; excerptDigest: string }
export interface DiscoveredInterface { id: string; protocol: string; method: string; path: string; summary: string; handler: { symbol?: string | null }; parameters: Array<{ name: string; in: 'path' | 'query' | 'header' | 'cookie' | 'body' | 'form' | 'unknown'; required: boolean; schema: Record<string, unknown> }>; requestBody?: { required: boolean; contentTypes: string[]; schema: Record<string, unknown> } | null; responses: Array<{ statusCode: string; contentTypes: string[]; schema: Record<string, unknown> }>; authentication: string[]; sideEffect: string; confidence: number; evidence: InterfaceEvidence[]; unresolved: string[] }
export interface InterfaceContract { apiVersion: 'looper.dev/interface-contract/v1'; kind: 'InterfaceContract'; metadata: { provider: string; model: string; harnessVersion: string; sourceDigest: string }; spec: { interfaces: DiscoveredInterface[]; unresolved: string[] } }
export interface SourceArchiveState { status: 'retained' | 'expired' | 'deleted' | 'unavailable'; expiresAt?: string | null; deletedAt?: string | null; deleteReason?: string | null; encryptedAtRest?: boolean; keyProtection?: 'windows-dpapi' | 'owner-key-file' | 'unavailable' | 'unknown' | null }
export interface SourceDiscovery { id: string; archiveName: string; sourceDigest: string; status: 'running' | 'completed' | 'failed'; provider: string; model: string; fileManifest: Array<{ path: string; bytes: number; sha256: string }>; excludedFiles: Array<{ path: string; reason: string }>; contract?: InterfaceContract | null; trace: Array<Record<string, unknown>>; error?: { code: string; message: string } | null; sourceArchive: SourceArchiveState; createdAt: string; completedAt?: string | null }

export interface CapacityBuildPlan { dockerfile: string; compose: string; startCommand: string; healthPath: string; servicePort: number; sourceRoot?: string; dependencies: string[]; unresolved: string[]; advisories?: string[]; checks?: Array<{ id: string; label: string; status: 'pass' | 'fixed' | 'fail'; detail: string }>; orderedMigrations?: string[]; evidence: Array<{ file: string; startLine: number; endLine: number }>; approved: boolean }
export interface CapacityAssertion { kind: 'status' | 'json-equals' | 'json-exists'; field: string; expected: unknown }
export interface CapacityScenarioStep { id: string; interfaceId: string; label: string; method: string; path: string; headers: Record<string, string>; body: unknown; extract: Record<string, string>; assertions: CapacityAssertion[]; sideEffect: string }
export interface CapacityDraft {
  build: CapacityBuildPlan;
  scenario: { steps: CapacityScenarioStep[]; resetStrategy: 'none' | 'compose-recreate' | 'custom'; resetCommand: string };
  slo: { minimumSuccessRate: number; maximumErrorRate: number; maximumTimeoutRate: number; p99Ms: number; p999Ms: number; confidenceLevel: 0.95; minimumSamples: number };
  targets: { sutIds: string[]; internalLoadGeneratorId: string; externalLoadGeneratorId: string; internalBaseUrls: Record<string, string>; externalBaseUrls: Record<string, string> };
  budget: { maxSeconds: number; maxAttempts: number; costCap: number; referenceRps: number; measurementSeconds: number };
}
export interface CapacityConstraint { code: string; group: string; label: string; status: 'pass' | 'fail'; blocking: boolean; detail: string }
export interface CapacityPreflightCheck { scope: 'sut' | 'load-generator'; targetId: string; network?: 'internal' | 'external'; passed: boolean; detail: string }
export interface CapacityPreflight { status?: 'pass' | 'fail'; draftRevision?: number; checkedAt?: string; checks?: CapacityPreflightCheck[]; failedSutIds?: string[]; generatorFailures?: string[] }
export type CapacityStudyStatus = 'draft' | 'queued' | 'deploying' | 'running' | 'resetting' | 'cleaning' | 'cancelling' | 'completed' | 'failed' | 'cancelled' | 'needs-attention';
export interface CapacityFrontier { status: string; confirmed_pass?: number | null; confirmed_fail?: number | null; width_ratio?: number | null; termination_reason?: string | null }
export interface CapacityReportTarget { target_id: string; label: string; status: string; attempt_count: number; valid_block_count: number; invalid_block_count: number; frontiers: Record<string, CapacityFrontier>; metrics: Array<{ metric: string; unit: string; raw?: number | null; status: string }> }
export interface CapacityReportNetwork { network: 'internal' | 'external'; experimentId: string; status?: string; terminationReason?: string; targets: CapacityReportTarget[]; comparisons: Array<Record<string, unknown>>; trajectory: Array<Record<string, unknown>>; evidence: Record<string, unknown> }
export interface CapacityStudy {
  id: string; discoveryId: string; discoveryName?: string; sourceDigest?: string; sourceArchive: SourceArchiveState;
  name: string; status: CapacityStudyStatus; revision: number; currentStep: number; draft: CapacityDraft;
  constraints: CapacityConstraint[]; readyToPreflight: boolean; preflight: CapacityPreflight;
  execution: { phases?: Array<{ id: string; status: string; detail?: string; at: string }>; selectedTargetIds?: string[]; excludedTargetIds?: string[]; activeTargetIds?: string[]; acknowledgedPartial?: boolean; budget?: CapacityDraft['budget']; costControl?: { currency: string; limit: number; scope: string; pricingStatus: string; estimatedIncrementalAmount: number; detail: string }; currentNetwork?: string; runs?: Array<{ network: string; experimentId: string; loadGeneratorTargetId: string; status: string; startedAt: string; completedAt?: string }>; liveMatrix?: Array<{ network: string; targetId: string; experimentId: string; currentLoad?: number | null; pointStatus: string; sloStatus: string; confirmedPass?: number | null; confirmedFail?: number | null }>; deployments?: Array<Record<string, unknown>>; cleanup?: Array<{ targetId: string; status: string; detail?: string; cleanedAt?: string }> };
  report?: { generatedAt: string; capacityUnit: string; confidenceLevel: number; networks: CapacityReportNetwork[]; decision: string } | null;
  error?: { code: string; message: string } | null; createdAt: string; updatedAt: string; startedAt?: string; completedAt?: string;
}

export interface Artifact { name: string; url: string; type?: string }
export interface Metric { name: string; value: number; unit?: string; baseline?: number; direction?: 'min' | 'max' }
export interface Evaluation { id: string; attemptId?: string; candidate: string; status: ExperimentStatus; phase?: string; phaseDetail?: string; score?: number; duration?: number; cost?: number; createdAt?: string; metrics?: Metric[]; artifacts?: Artifact[]; error?: string }
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
  metricDefinitions?: Record<string, MetricDefinition>;
  resultSections?: BenchmarkResultSection[];
  config?: Record<string, unknown>; evaluations?: Evaluation[]; artifacts?: Artifact[];
  activePhase?: string; activePhaseDetail?: string;
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
export type MetricRole = 'primary_outcome' | 'hard_gate' | 'guardrail' | 'cost_efficiency' | 'stability' | 'diagnostic' | 'context';
export type MetricVisibility = 'summary' | 'detail' | 'expert' | 'hidden';
export type MetricDisplayFormat = 'number' | 'percent' | 'duration' | 'bytes' | 'throughput' | 'boolean';
export interface MetricPresentation {
  userLabel?: string;
  userDescription?: string;
  roles?: MetricRole[];
  defaultVisibility?: MetricVisibility;
  displayFormat?: MetricDisplayFormat;
  displayPrecision?: number;
  glossary?: string;
}
export interface MetricDefinition {
  unit?: string;
  direction?: 'minimize' | 'maximize' | 'none';
  kind?: 'sample' | 'aggregate' | 'counter' | 'boolean';
  required?: boolean;
  minimumSamples?: number;
  description?: string;
  presentation?: MetricPresentation;
}
export interface BenchmarkResultSection {
  id: string;
  label: string;
  description?: string;
  metrics: string[];
}
export interface BenchmarkWorkload {
  id: string;
  name: string;
  metrics?: Record<string, MetricDefinition>;
}
export interface BenchmarkSelectionDefaults {
  repeats: number;
  timeout: number;
  seed: number;
}

export interface Benchmark {
  id: string; key?: string; name: string; description?: string; category?: string; version?: string; metrics?: string[];
  metricDefinitions?: Record<string, MetricDefinition>;
  resultSections?: BenchmarkResultSection[];
  workloads?: BenchmarkWorkload[];
  executionModel?: BenchmarkExecutionModel;
  inputs?: BenchmarkInputDeclaration[];
  infrastructure?: Record<string, unknown>;
  auditPolicy?: Record<string, unknown>;
  selectionDefaults?: BenchmarkSelectionDefaults;
  executionPolicy?: Record<string, unknown>;
  cases?: number; updatedAt?: string; tags?: string[]; license?: string; runnable?: boolean; selectable?: boolean; executionStatus?: string;
  executionBlocker?: string; executionBlockerReason?: string;
  deploymentRequirements?: string[]; provisionedCapabilities?: string[]; provisioning?: Record<string, unknown>; packageReady?: boolean; packageDigest?: string;
  decisionQuestion?: string; primaryMetric?: string; scenario?: ScenarioContract;
  selectionReady?: boolean; singleNodeReady?: boolean;
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
  packageDigest?: string; packageReady?: boolean; benchmarkKey?: string; createdAt: string; updatedAt: string; registeredAt?: string;
}
export interface Target {
  id: string; name: string; type?: string; provider?: string; orderId?: string; endpoint?: string;
  status?: 'online' | 'offline' | 'degraded' | 'unknown' | 'inventory';
  lifecycleStatus?: 'active' | 'missing' | 'archived'; framework?: string; version?: string;
  hardware?: string; lastSeenAt?: string; lastInventorySeenAt?: string; missingSince?: string;
  inventoryMissCount?: number; archivedAt?: string; archiveReason?: string;
  tags?: string[]; runnable?: boolean;
  credentialsRemembered?: boolean;
  sshAutomation?: { status: 'manual' | 'waiting_endpoint' | 'connected' | 'failed'; deployment?: string; message?: string };
  deployment?: { active?: boolean; status?: string; workerId?: string; remotePid?: number; remotePort?: number; transport?: string; restartSafe?: boolean; deployedAt?: string };
  connectionTest?: { status: 'connected'; testedAt: string; hostKeySha256?: string };
  fingerprint?: {
    processor?: string; logical_cpu_count?: number; memory_gib?: number; instance_type?: string; region?: string;
    system?: string; release?: string; architecture?: string; host_key_sha256?: string; host_key_type?: string;
  };
}
export interface BenchmarkTargetConstraint {
  code: string; field: string; required: unknown; actual: unknown; message: string;
}
export interface BenchmarkTargetRequirementSummary {
  osFamilies: string[]; architectures: string[]; minimumLogicalCpus?: number;
  minimumMemoryGiB?: number; capabilities: string[];
}
export interface BenchmarkTargetEnvironment {
  id: string; label: string; compatibleCount: number; targets: Target[];
}
export interface BenchmarkTargetOptions {
  benchmarkId: string; version: string; topology?: string; machineCount: 1;
  nodeGroup: { id: string; role: string; requirements: Record<string, unknown>; summary: BenchmarkTargetRequirementSummary };
  environments: BenchmarkTargetEnvironment[];
  rejectedSummary: Array<{ code: string; message: string; count: number }>;
}
export type DestroyedResourceKind = 'instance' | 'system-disk' | 'local-disk' | 'public-ip' | 'vpc' | 'subnet' | 'security-group';
export interface DestroyedResource {
  kind: DestroyedResourceKind; id: string; released: boolean; note?: string;
}
export interface TargetDestroyPreview {
  targetId: string; provider: CloudProviderId; region: string; instanceId: string;
  instanceName: string; acknowledgement: string; resources: DestroyedResource[];
}
export interface TargetDestroyResult {
  targetId: string; provider: CloudProviderId; instanceId: string; requestId?: string;
  status: string; resources: DestroyedResource[];
}
export interface AnalysisData {
  mode?: 'optimization' | 'selection'; targets?: SelectionTargetResult[]; comparisons?: SelectionComparison[];
  pareto?: Array<{ id?: string; candidate: string; score: number; cost: number; latency?: number; rank?: number | null; feasible?: boolean; objectives?: Record<string, number> }>;
  benchtrust?: BenchTrustData;
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

export type BenchTrustStatus = 'available' | 'partial' | 'insufficient_evidence' | 'unavailable';
export interface BenchTrustReferenceEnvironment {
  environment_id: string;
  environment_fingerprint?: Record<string, unknown> | null;
  eligible: boolean;
  excluded_reason?: string | null;
  reference_value?: number | null;
  baseline_value?: number | null;
  benefit?: number | null;
  benefit_lower?: number | null;
  benefit_upper?: number | null;
  repeat_count?: number | null;
  valid?: boolean | null;
  invalid_reason?: string | null;
}
export interface BenchTrustReferenceValidity {
  status: BenchTrustStatus; method: string;
  valid_environment_count: number; eligible_environment_count: number; excluded_environment_count: number;
  rate: number | null; confidence_interval: [number, number] | null;
  expected_direction: string; minimum_effect: number;
  environment_results: BenchTrustReferenceEnvironment[];
  criteria: string[]; limitations: string[];
}
export interface BenchTrustRankAxis {
  axis: string; scoring_formula_ids?: string[] | null;
  slice_count: number; candidate_count: number; comparison_count: number; method: string;
  median_tau: number | null; minimum_tau: number | null; maximum_tau: number | null;
  pairwise_flip_rate: number | null; tie_count: number; limitations: string[];
}
export interface BenchTrustRankStability {
  status: BenchTrustStatus; axes: BenchTrustRankAxis[]; limitations: string[];
}
export interface BenchTrustTaskContributor {
  task_id: string; weight: number; contribution: number; contribution_share: number;
}
export interface BenchTrustTaskLeverage {
  status: BenchTrustStatus; scoring_formula: string | null; aggregation_method: string | null;
  maximum_contribution_share: number | null; dominant_task: string | null;
  top_contributors: BenchTrustTaskContributor[];
  leave_one_out: { maximum_rank_shift: number | null; winner_changed: boolean | null; task_shifts: Record<string, number> };
  limitations: string[];
}
export interface BenchTrustEnvironmentFactor {
  factor: string; group_count: number; sample_count: number;
  associated_variance_ratio: number | null; confidence_interval: [number, number] | null; missing_rate: number;
}
export interface BenchTrustEnvironmentSensitivity {
  status: BenchTrustStatus; method: string; analysis_unit: string; sample_count?: number; controls?: string[];
  total_explained_ratio: number | null; factors: BenchTrustEnvironmentFactor[]; residual_ratio: number | null;
  warnings: string[]; limitations: string[]; association_only: boolean;
}
export interface BenchTrustData {
  schemaVersion: string; methodVersion: string; status: BenchTrustStatus;
  referenceValidityRate: BenchTrustReferenceValidity;
  rankStability: BenchTrustRankStability;
  taskLeverage: BenchTrustTaskLeverage;
  environmentSensitivity: BenchTrustEnvironmentSensitivity;
  evidence: { sample_count?: number; target_count?: number; distinct_dates?: number; distinct_workloads?: number };
  limitations: string[]; inputDigest: string; policyDigest: string;
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
  tags?: Record<string, string>; managed?: boolean;
}
export interface CloudSubnet {
  provider: CloudProviderId; region: string; zone: string; vpcId: string; id: string; name: string;
  cidrBlock?: string; availableIpCount?: number; isDefault: boolean; tags: Record<string, string>; managed: boolean;
}
export interface InstanceNetworkResolveRequest {
  region: string; instanceType: string; zone?: string; vpcId?: string; subnetId?: string;
}
export interface InstanceNetworkResolution {
  provider: CloudProviderId; region: string; instanceType: string; zone: string; eligibleZones: string[];
  vpc: CloudVpc; subnet: CloudSubnet; zoneAutomaticallySelected: boolean;
  securityGroup?: CloudSecurityGroup; vpcAction: 'reused' | 'created'; subnetAction: 'reused' | 'created';
  securityGroupAction: 'reused' | 'created' | 'selection-required'; warnings: string[];
}
export interface CloudSecurityGroup {
  provider: CloudProviderId; region: string; id: string; name: string; description?: string;
  vpcId?: string; isDefault: boolean; recommended: boolean; tags: Record<string, string>; managed: boolean;
}
export interface CloudKeyPair {
  provider: CloudProviderId; region: string; id: string; name: string; description?: string;
  createdAt?: string; associatedInstanceCount: number;
}
export interface CloudInstanceType {
  provider: CloudProviderId; region: string; id: string; family?: string; cpu: number; memoryGib: number;
  typeLabel?: string; familyLabel?: string;
  selectionClass?: InstanceSelectionClass; typeKind?: string; familyToken?: string;
  gpu?: number; gpuModel?: string; gpuMemoryGib?: number; architecture?: string;
  networkBandwidthRxGbps?: number; networkBandwidthTxGbps?: number; networkPpsRx?: number; networkPpsTx?: number;
  localStorageCount?: number; localStorageCapacityGib?: number; localStorageCategory?: string;
  zones: string[]; available?: boolean; attributes?: Record<string, unknown>;
}
export type InstanceSelectionClass = 'x86' | 'arm' | 'heterogeneous' | 'bare-metal' | 'hpc' | 'other';
export interface InstanceTypeFamilyFacet { value: string; label: string; count: number; generation?: number | null; }
export interface InstanceTypeKindFacet { value: string; label: string; count: number; families: InstanceTypeFamilyFacet[]; }
export interface InstanceTypeArchitectureFacet {
  value: InstanceSelectionClass; label: string; count: number; types: InstanceTypeKindFacet[];
}
export interface InstanceTypeFacets { architectures: InstanceTypeArchitectureFacet[]; }
export type SelectionScenario = 'web-api' | 'microservices-rpc' | 'database' | 'cache' | 'search-logs' |
  'big-data-messaging' | 'game' | 'video' | 'ai' | 'development-test' | 'other';
export interface SelectionAdvisorRequest {
  provider: 'alibaba' | 'tencent'; region: string; zone?: string; primaryScenario: SelectionScenario;
  coLocatedComponents: SelectionScenario[]; sizingMode: 'exact' | 'unknown'; exactCpu?: number;
  exactMemoryGib?: number; workloadScale?: string; minimumGpuCount: number;
  localStorage: 'required' | 'not-required' | 'unknown'; minimumNetworkBandwidthGbps?: number;
  minimumNetworkPps?: number; codeAvailability: 'available' | 'unavailable' | 'unknown';
  architecture: 'x86' | 'arm' | 'unknown'; query?: string; offset: number; limit: number;
  architectureClass?: InstanceSelectionClass; typeKind?: string; familyToken?: string;
}
export interface SelectionExclusionStage {
  code: string; label: string; before: number; after: number; removed: number;
}
export interface AdvisedCloudInstanceType extends CloudInstanceType {
  matchTier: 'preferred' | 'suitable' | 'other'; reasons: string[]; warnings: string[];
}
export interface SelectionAdvisorResponse {
  provider: 'alibaba' | 'tencent'; region: string; zone?: string; items: AdvisedCloudInstanceType[]; total: number;
  eligibleTotal: number; offset: number; limit: number; nextOffset?: number; exclusionStages: SelectionExclusionStage[];
  mostRestrictiveStage?: SelectionExclusionStage; source: 'live' | 'cache' | 'stale-cache';
  fetchedAt: string; expiresAt: string; stale: boolean; warning?: string;
  instanceTypeFacets?: InstanceTypeFacets;
}
export interface CloudImage {
  provider: CloudProviderId; region: string; id: string; name: string; platform?: string; architecture?: string;
  imageType?: string; sizeGib?: number; createdAt?: string; available?: boolean; attributes?: Record<string, unknown>;
}
export interface CloudCatalogResponse<T> {
  provider: CloudProviderId; resourceType: string; items: T[]; total: number;
  offset: number; limit: number; nextOffset?: number;
  source: 'live' | 'cache' | 'stale-cache'; fetchedAt: string; expiresAt: string; stale: boolean; warning?: string;
  instanceTypeFacets?: InstanceTypeFacets;
}
export interface CloudPurchaseSpec {
  provider: CloudProviderId; region: string; zone: string; instanceType: string; cpu?: number; memoryGib?: number;
  imageId: string; instanceName: string; count: number; billingMode: 'postpaid'; vpcId: string; subnetId: string;
  securityGroupIds: string[]; keyPairId?: string; systemDiskType?: string; systemDiskGib: number;
  publicIp: boolean; internetBandwidthMbps: number; tags: Record<string, string>;
}
export interface CloudSshDefaults {
  username: string; port: number; authMethod: 'password' | 'private-key'; password: string;
  passwordConfigured: boolean; privateKeyConfigured: boolean;
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
