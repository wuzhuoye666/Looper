import { Activity, AlertTriangle, Info, Lightbulb } from 'lucide-react';
import { formatNumber } from '../lib/format';
import type { Evaluation, VariabilityData, VariabilityGroupReport, VariabilityRecommendation, VariabilityStatus } from '../lib/types';

const statusLabels: Record<VariabilityStatus, string> = {
  stable: '稳定', warning: '需关注', unstable: '不稳定', insufficient_evidence: '证据不足',
};
const statusClass: Record<VariabilityStatus, string> = {
  stable: 'ok', warning: 'warn', unstable: 'bad', insufficient_evidence: 'muted',
};
const priorityLabels: Record<string, string> = { high: '高优先级', medium: '中优先级', low: '低优先级' };
const workloadLabels: Record<string, string> = {
  cpu: 'CPU 素数计算', memory: '内存吞吐', thread: '线程调度', mutex: '互斥锁竞争',
  oss_performance_mediawiki_mlp: 'DCPerf MediaWiki', phpbench: 'PHPBench',
};

type GroupedRecommendation = VariabilityRecommendation & { workloads: string[] };
type RunRoundMap = Map<string, number>;

export function VariabilityPanel({ data, evaluations = [] }: { data: VariabilityData; evaluations?: Evaluation[] }) {
  if (data.status === 'unavailable') {
    return <section className="panel optimization-advice-empty"><div className="panel-heading"><div><h2>优化建议</h2><p>建议只从 Benchmark 合同生成</p></div></div>
      <p className="decision-copy">{data.reason || '该 Benchmark 未声明优化建议规则。'}</p></section>;
  }
  if (!data.groups?.length) {
    return <section className="panel optimization-advice-empty"><div className="panel-heading"><div><h2>优化建议</h2><p>基于成功且通过必要门禁的原始 Benchmark 轮次</p></div></div>
      <p className="decision-copy">暂无有效原始轮次。失败、超时或未通过门禁的证据仍保留，但不会进入统计。</p></section>;
  }
  const recommendations = dedupeRecommendations(data.groups);
  const runRounds = new Map(evaluations.flatMap(evaluation =>
    (evaluation.runs || []).map(run => [run.attemptId, run.round] as const),
  ));
  return <div className="variability-report optimization-advice-report">
    <section className="panel optimization-advice-intro">
      <div className="panel-heading"><div><h2><Lightbulb size={17} /> 优化建议</h2><p>只读诊断，不自动修改系统参数</p></div></div>
      <div className="inline-alert neutral"><Info size={16} /><span>当前建议依据 Benchmark 原始数据与合同门禁生成；单目标实验不形成性能优劣、领先或采购结论。</span></div>
      {data.diagnosticContractDigest && <span className="cell-meta">诊断合同：{data.diagnosticContractDigest}</span>}
    </section>
    {data.groups.map(group => <GroupReport key={`${group.targetId}-${group.workloadId}`} group={group} runRounds={runRounds} />)}
    <RecommendationSection title="合同诊断与复测建议" items={recommendations} />
    <RecommendationSection title="研究级建议" items={(data.studyRecommendations || []).map(item => ({ ...item, workloads: [] }))} />
  </div>;
}

function GroupReport({ group, runRounds }: { group: VariabilityGroupReport; runRounds: RunRoundMap }) {
  const stats = group.distribution;
  const anomalyReasons = describeAnomalies(group, runRounds);
  const facts: Array<[string, string]> = [
    ['有效样本', `${stats.count} 轮`],
    ['均值', `${formatNumber(stats.mean)} ${group.unit}`],
    ['中位数', `${formatNumber(stats.median)} ${group.unit}`],
    ['最小 / 最大', `${formatNumber(stats.minimum)} / ${formatNumber(stats.maximum)} ${group.unit}`],
    ['变异系数 CV', stats.coefficientOfVariation == null ? '—' : formatNumber(stats.coefficientOfVariation, 4)],
    ['未过门禁', `${group.invalidAttemptCount || 0} 轮（不计入统计）`],
  ];
  return <section className="panel variability-panel optimization-workload-card">
    <div className="panel-heading"><div>
      <h2><Activity size={16} /> {workloadLabels[group.workloadId] || group.workloadId}</h2>
      <p>{group.targetId} · 主指标 {group.metric} · {group.direction === 'minimize' ? '越低越好' : '越高越好'}</p>
    </div><span className={`tag ${statusClass[group.status]}`}>{statusLabels[group.status]}</span></div>
    <div className="optimization-facts">{facts.map(([label, value]) => <div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
    {anomalyReasons.length ? <div className="optimization-reasons"><strong>异常原因</strong><ul>{anomalyReasons.map((reason, index) => <li key={index}><AlertTriangle size={13} />{reason}</li>)}</ul></div> : null}
  </section>;
}

function describeAnomalies(group: VariabilityGroupReport, runRounds: RunRoundMap): string[] {
  if (group.status === 'stable') return [];
  const outliers = group.runs.filter(run => run.label === 'slow_outlier' || run.label === 'fast_outlier');
  if (outliers.length) {
    return outliers.map(run => {
      const round = runRounds.get(run.runId);
      const slow = run.label === 'slow_outlier';
      const lowerIsWorse = group.direction === 'maximize';
      const low = slow === lowerIsWorse;
      const metricKind = group.direction === 'minimize' ? '延迟' : '吞吐';
      const median = group.distribution.median;
      const difference = median === 0 ? null : Math.abs((run.value - median) / median) * 100;
      const prefix = round == null ? '某一轮' : `第 ${round} 轮`;
      const comparison = difference == null
        ? ''
        : `，比中位数${low ? '低' : '高'} ${formatNumber(difference, 2)}%`;
      return `${prefix}的主指标${metricKind}偏${low ? '低' : '高'}（${formatNumber(run.value)} ${group.unit}${comparison}）`;
    });
  }
  return group.stability?.reasons || [];
}

function dedupeRecommendations(groups: VariabilityGroupReport[]): GroupedRecommendation[] {
  const merged = new Map<string, GroupedRecommendation>();
  for (const group of groups) {
    for (const recommendation of group.recommendations || []) {
      const key = recommendation.ruleId || `${recommendation.action}:${recommendation.rationale}`;
      const current = merged.get(key);
      const workload = workloadLabels[group.workloadId] || group.workloadId;
      if (current) {
        if (!current.workloads.includes(workload)) current.workloads.push(workload);
      } else {
        merged.set(key, { ...recommendation, workloads: [workload] });
      }
    }
  }
  return [...merged.values()];
}

function RecommendationSection({ title, items }: { title: string; items: GroupedRecommendation[] }) {
  if (!items.length) return null;
  return <section className="panel optimization-recommendations"><div className="panel-heading"><div><h2>{title}</h2><p>来源：Benchmark 合同</p></div></div>
    <ul className="recommendation-list">{items.map(item => <li key={item.ruleId || item.action} className={`priority-${item.priority}`}>
      <span className={`tag ${item.priority === 'high' ? 'bad' : item.priority === 'medium' ? 'warn' : 'muted'}`}>{priorityLabels[item.priority] || item.priority}</span>
      <div><strong>{item.action}</strong><p className="cell-meta">{item.rationale}</p>{item.workloads.length ? <span className="cell-meta">涉及：{item.workloads.join('、')}</span> : null}</div>
    </li>)}</ul>
  </section>;
}
