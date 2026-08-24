import type { Benchmark, BenchmarkExecutionModel, BenchmarkInputDeclaration, SelectionScenario } from './types';
import { SELECTION_SCENARIOS, type SelectionScenarioOption } from './selectionScenarios';

interface BenchmarkCopy {
  name: string;
  description: string;
  decisionQuestion: string;
  scenario?: SelectionScenario;
}

const BENCHMARK_COPY: Record<string, BenchmarkCopy> = {
  'benchbase.smallbank.postgres': {
    name: 'BenchBase SmallBank（PostgreSQL）',
    description: '使用 BenchBase SmallBank 事务负载，对比 PostgreSQL 数据库服务器在延迟目标内的有效事务处理能力。',
    decisionQuestion: '在满足 P99 延迟目标的前提下，哪种服务器规格能以更低的小时成本完成更多银行事务？',
    scenario: 'database',
  },
  'looper.fixture.config-driven': {
    name: '配置驱动适配器合同验证套件',
    description: '验证完整测试包的下发、环境准备、任务执行、结果标准化、证据采集和结果展示流程。',
    decisionQuestion: '目标机器能否在无需预装套件脚本或手工配置环境的情况下，接收并运行完整测试包？',
    scenario: 'development-test',
  },
  'dcperf.mediawiki.closed-loop': {
    name: 'DCPerf MediaWiki 单机闭环测试',
    description: '在同一台云服务器上运行 MediaWiki 全栈服务和负载生成器，测试生产型网站场景的请求处理能力。',
    decisionQuestion: '当 MediaWiki 全部组件共享一台云服务器时，各服务器规格每秒能完成多少个成功请求？',
    scenario: 'web-api',
  },
  'looper.demo.compression': {
    name: '确定性压缩循环测试',
    description: '使用固定数据进行 zlib 压缩，在确保逐字节无损还原和合理压缩率的同时，对比吞吐量与延迟。',
    decisionQuestion: '哪种服务器和压缩参数能在确保无损还原及合理压缩率的同时，提供最高的数据吞吐量？',
    scenario: 'development-test',
  },
  'looper.phoronix-phpbench': {
    name: 'Phoronix 测试套件 / PHPBench',
    description: '自动部署固定版本的 Phoronix 测试套件和 PHPBench，用于对比 PHP 解释器及单机 CPU 执行性能。',
    decisionQuestion: '在固定的 PHPBench 配置和重复策略下，哪台目标机器能获得更高的测试得分？',
    scenario: 'development-test',
  },
  'looper.sysbench': {
    name: 'Sysbench 系统性能测试',
    description: '自动部署 Sysbench，测试 CPU、内存、线程调度和互斥锁竞争等基础系统性能。',
    decisionQuestion: '在受控测试条件下，哪种服务器规格具有更好的 CPU 吞吐、内存带宽、线程调度和互斥锁性能？',
    scenario: 'development-test',
  },
};

const WORKLOAD_CLASS_SCENARIOS: Record<string, SelectionScenario> = {
  'transactional-database': 'database', database: 'database', oltp: 'database',
  'web-full-stack': 'web-api', web: 'web-api', 'web-api': 'web-api',
  'microservices-rpc': 'microservices-rpc', cache: 'cache',
  'search-logs': 'search-logs', 'big-data-messaging': 'big-data-messaging',
  game: 'game', video: 'video', ai: 'ai',
  'integration-validation': 'development-test', 'cpu-compression': 'development-test',
  microbenchmark: 'development-test', 'development-test': 'development-test',
};

const EXECUTION_MODEL_LABELS: Record<BenchmarkExecutionModel, string> = {
  'batch-suite': '批量测试套件', 'service-stack': '服务栈', database: '数据库', storage: '存储',
  network: '网络', distributed: '分布式', accelerator: '加速器', custom: '自定义',
};

const TOPOLOGY_LABELS: Record<string, string> = {
  'single-node': '单机', 'client-server': '客户端 / 服务端', 'multi-node': '多机', 'closed-loop': '单机闭环',
};

const CAPABILITY_LABELS: Record<string, string> = {
  linux: 'Linux', windows: 'Windows', python: 'Python', container: '容器',
  'local-process': '本地进程', benchbase: 'BenchBase', postgresql: 'PostgreSQL',
  'phoronix-test-suite': 'Phoronix 测试套件', 'php-cli': 'PHP 命令行', unzip: '解压工具', sysbench: 'Sysbench',
};

const INPUT_KIND_LABELS: Record<BenchmarkInputDeclaration['kind'], string> = {
  dataset: '数据集', artifact: '制品', config: '配置', endpoint: '服务端点',
  secret: '密钥', device: '设备', topology: '拓扑',
};

const METRIC_LABELS: Record<string, string> = {
  offered_tps: '施加负载', attempted_tps: '尝试事务速率', offered_requests: '施加请求数',
  started_requests: '已开始请求数', completed_requests: '已完成请求数',
  offered_load_achieved_ratio: '负载达成率', rate_limiter_lag_ratio: '限流滞后率',
  client_headroom_ratio: '客户端余量', committed_tps: '提交吞吐量', committed_transactions: '提交事务数',
  timeout_count: '超时数量', timeout_ratio: '超时率', latency_p50_ms: 'P50 延迟',
  latency_p95_ms: 'P95 延迟', latency_p99_ms: 'P99 延迟', latency_p999_ms: 'P99.9 延迟',
  latency_max_ms: '最大延迟', latency_avg_ms: '平均延迟', abort_ratio: '中止率', retry_ratio: '重试率',
  error_ratio: '错误率', closed_loop_successful_rps: '成功请求率', wrk_rps: '客户端施加速率',
  successful_requests: '成功请求数', failed_request_ratio: '失败请求率', cpu_utilization_p95: 'CPU 利用率 P95',
  throughput_mib_s: '吞吐量', latency_ms: '单次测试延迟', compression_ratio: '压缩率',
  roundtrip_ok: '无损往返校验', output_bytes: '输出数据量', events_per_sec: '每秒事件数',
  sysbench_run_ok: '测试运行成功',
  fixture_score: '处理条目', phpbench_score: 'PHPBench 综合得分',
  phpbench_score_sample: 'PHPBench 单次得分', sample_count: '样本数量',
  pts_run_ok: '测试运行成功', profile_version_match: '测试配置版本匹配',
  score: '综合得分', goodput: '有效吞吐量',
};

export function benchmarkName(benchmark: Pick<Benchmark, 'id' | 'name'>): string {
  return BENCHMARK_COPY[benchmark.id]?.name || benchmark.name;
}

export function benchmarkDescription(benchmark: Pick<Benchmark, 'id' | 'description'>): string {
  return BENCHMARK_COPY[benchmark.id]?.description || benchmark.description || '暂无套件内容说明。';
}

export function benchmarkDecisionQuestion(benchmark: Pick<Benchmark, 'id' | 'decisionQuestion' | 'scenario'>): string {
  return BENCHMARK_COPY[benchmark.id]?.decisionQuestion
    || benchmark.decisionQuestion
    || benchmark.scenario?.decision_question
    || '暂无选型问题说明。';
}

export function benchmarkScenario(benchmark: Pick<Benchmark, 'id' | 'scenario'>): SelectionScenarioOption {
  const explicit = BENCHMARK_COPY[benchmark.id]?.scenario;
  const workloadClass = benchmark.scenario?.workload_class?.toLowerCase();
  const scenarioId = explicit || (workloadClass ? WORKLOAD_CLASS_SCENARIOS[workloadClass] : undefined) || 'other';
  return SELECTION_SCENARIOS.find(item => item.id === scenarioId) || SELECTION_SCENARIOS[SELECTION_SCENARIOS.length - 1];
}

export function benchmarkMetricLabel(benchmark: Pick<Benchmark, 'metricDefinitions'>, metric?: string): string {
  if (!metric) return '未声明';
  return benchmark.metricDefinitions?.[metric]?.presentation?.userLabel || METRIC_LABELS[metric] || metric;
}

export function benchmarkMetricLabels(benchmark: Pick<Benchmark, 'metrics' | 'metricDefinitions'>): string[] {
  return (benchmark.metrics || []).map(metric => benchmarkMetricLabel(benchmark, metric));
}

export function executionModelLabel(model?: BenchmarkExecutionModel): string {
  return model ? EXECUTION_MODEL_LABELS[model] : '自定义';
}

export function topologyLabel(topology?: string): string {
  return topology ? TOPOLOGY_LABELS[topology] || topology : '未声明';
}

export function capabilityLabel(capability: string): string {
  return CAPABILITY_LABELS[capability.toLowerCase()] || capability;
}

export function inputKindLabel(kind: BenchmarkInputDeclaration['kind']): string {
  return INPUT_KIND_LABELS[kind];
}

export function executionBlockerLabel(benchmark: Pick<Benchmark, 'id' | 'executionBlockerReason'>): string | undefined {
  if (!benchmark.executionBlockerReason) return undefined;
  if (benchmark.id === 'benchbase.smallbank.postgres') {
    return '该套件需要分别部署客户端和服务端；当前 Looper 暂不支持客户端 / 服务端多机编排。';
  }
  return benchmark.executionBlockerReason;
}
