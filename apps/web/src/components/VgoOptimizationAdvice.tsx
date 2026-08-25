import { Activity, AlertTriangle, CheckCircle2, Info, Lightbulb } from 'lucide-react';
import { formatNumber } from '../lib/format';
import type { Evaluation, Metric } from '../lib/types';

const workloadLabels: Record<string, string> = {
  matmul: 'Matmul',
  '7z': '7-Zip',
  lbm: 'LBM',
  sad: 'SAD',
};

type Advice = {
  priority: 'high' | 'medium' | 'low';
  action: string;
  rationale: string;
};

const priorityLabels: Record<Advice['priority'], string> = {
  high: '高优先级',
  medium: '中优先级',
  low: '低优先级',
};

export function VgoOptimizationAdvice({ evaluations }: { evaluations: Evaluation[] }) {
  const completed = evaluations.filter(item => item.status === 'completed' && item.metrics?.length);
  if (!completed.length) {
    return <section className="panel optimization-advice-empty">
      <div className="panel-heading"><div><h2>优化建议</h2><p>基于成功且通过必要门禁的原始 Benchmark 轮次</p></div></div>
      <p className="decision-copy">暂无有效原始轮次。失败、超时或未通过门禁的证据仍保留，但不会进入建议判断。</p>
    </section>;
  }

  return <div className="variability-report optimization-advice-report">
    <section className="panel optimization-advice-intro">
      <div className="panel-heading"><div><h2><Lightbulb size={17} /> 优化建议</h2><p>基于成功且通过必要门禁的原始 VGO 轮次</p></div></div>
      <div className="inline-alert neutral"><Info size={16} /><span>建议由本次 VGO 基线组、优化组与 rollback 实测数据生成；这里只给出部署建议，不会自动修改服务器参数。</span></div>
    </section>
    {completed.map(item => <VgoWorkloadAdvice key={item.id} evaluation={item} />)}
  </div>;
}

function VgoWorkloadAdvice({ evaluation }: { evaluation: Evaluation }) {
  const metrics = metricMap(evaluation.metrics || []);
  const baselineCv = numberMetric(metrics, 'runtime_cv');
  const optimizedCv = numberMetric(metrics, 'optimized_runtime_cv');
  const cvReduction = numberMetric(metrics, 'cv_reduction_ratio');
  const medianImprovement = numberMetric(metrics, 'median_improvement_ratio');
  const p95Improvement = numberMetric(metrics, 'p95_improvement_ratio');
  const rollbackDrift = numberMetric(metrics, 'rollback_median_drift_ratio');
  const correctness = numberMetric(metrics, 'correctness_rate');
  const cpuSteal = numberMetric(metrics, 'cpu_steal_p95_percent');
  const sampleCount = numberMetric(metrics, 'sample_count');
  const runOk = booleanMetric(metrics, 'run_ok');
  const advice = buildAdvice({ runOk, correctness, cvReduction, medianImprovement, p95Improvement, rollbackDrift });
  const workload = workloadLabels[evaluation.workload || ''] || evaluation.workload || evaluation.candidate;
  const passed = runOk === true && (correctness == null || correctness >= 1);

  const facts: Array<[string, string]> = [
    ['基线 CV', percent(baselineCv)],
    ['优化组 CV', percent(optimizedCv)],
    ['波动降低', signedPercent(cvReduction)],
    ['中位耗时改善', signedPercent(medianImprovement)],
    ['P95 耗时改善', signedPercent(p95Improvement)],
    ['回滚后漂移', percent(rollbackDrift)],
    ['正确样本率', percent(correctness)],
    ['CPU steal P95', cpuSteal == null ? '—' : `${formatNumber(cpuSteal, 2)}%`],
    ['有效样本', sampleCount == null ? '—' : `${formatNumber(sampleCount, 0)} 个`],
  ];

  return <section className="panel variability-panel optimization-workload-card">
    <div className="panel-heading"><div>
      <h2><Activity size={16} /> {workload}</h2>
      <p>{evaluation.candidate} · VGO 基线/优化交替执行与 rollback 结果</p>
    </div><span className={`tag ${passed ? 'ok' : 'bad'}`}>{passed ? '门禁通过' : '门禁未通过'}</span></div>
    <div className="optimization-facts">{facts.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
    <div className="optimization-recommendations">
      <ul className="recommendation-list"><li className={`priority-${advice.priority}`}>
        <span className={`tag ${advice.priority === 'high' ? 'bad' : advice.priority === 'medium' ? 'warn' : 'ok'}`}>{priorityLabels[advice.priority]}</span>
        <div><strong>{advice.action}</strong><p className="cell-meta">{advice.rationale}</p></div>
        {passed ? <CheckCircle2 size={17} aria-label="门禁通过" /> : <AlertTriangle size={17} aria-label="门禁未通过" />}
      </li></ul>
    </div>
  </section>;
}

function buildAdvice(values: {
  runOk: boolean | null;
  correctness: number | null;
  cvReduction: number | null;
  medianImprovement: number | null;
  p95Improvement: number | null;
  rollbackDrift: number | null;
}): Advice {
  if (values.runOk !== true || (values.correctness != null && values.correctness < 1)) {
    return {
      priority: 'high',
      action: '暂不采用优化配置，先处理执行或正确性门禁。',
      rationale: '原始 VGO 脚本未完整成功，或存在未通过正确性校验的样本。',
    };
  }
  if (values.cvReduction == null) {
    return {
      priority: 'medium',
      action: '保留当前结果并补充一轮可比较的基线/优化采集。',
      rationale: '当前证据未包含可用于判断波动改善比例的完整指标。',
    };
  }
  if (values.cvReduction <= 0) {
    return {
      priority: 'high',
      action: '保留基线配置，调整优化参数后重新验证。',
      rationale: `优化组波动未降低（${signedPercent(values.cvReduction)}），不建议直接部署当前优化参数。`,
    };
  }
  if ((values.medianImprovement ?? 0) < 0 || (values.p95Improvement ?? 0) < 0) {
    return {
      priority: 'medium',
      action: '波动有所降低，但先修正耗时回退再部署。',
      rationale: `波动降低 ${percent(values.cvReduction)}，但中位或 P95 耗时出现回退。`,
    };
  }
  if ((values.rollbackDrift ?? 0) > 0.05) {
    return {
      priority: 'medium',
      action: '先复核 rollback 稳定性，再决定是否部署优化配置。',
      rationale: `回滚后中位漂移为 ${percent(values.rollbackDrift)}，超过 5% 复核阈值。`,
    };
  }
  return {
    priority: 'low',
    action: '建议保留当前 VGO 优化配置，并在服务器部署后扩大轮次复核。',
    rationale: `优化组波动降低 ${percent(values.cvReduction)}，且中位耗时、P95 耗时与 rollback 门禁均未发现回退。`,
  };
}

function metricMap(metrics: Metric[]) {
  return new Map(metrics.map(metric => [metric.name, metric.value] as const));
}

function numberMetric(metrics: Map<string, number | boolean>, name: string) {
  const value = metrics.get(name);
  return typeof value === 'number' ? value : null;
}

function booleanMetric(metrics: Map<string, number | boolean>, name: string) {
  const value = metrics.get(name);
  return typeof value === 'boolean' ? value : null;
}

function percent(value: number | null) {
  return value == null ? '—' : `${formatNumber(value * 100, 2)}%`;
}

function signedPercent(value: number | null) {
  if (value == null) return '—';
  return `${value > 0 ? '+' : ''}${formatNumber(value * 100, 2)}%`;
}
