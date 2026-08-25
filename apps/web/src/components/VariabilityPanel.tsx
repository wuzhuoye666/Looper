import { Activity, AlertTriangle, Info, Lightbulb } from 'lucide-react';
import { formatNumber } from '../lib/format';
import type { VariabilityData, VariabilityGroupReport, VariabilityRecommendation, VariabilityStatus } from '../lib/types';

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

export function VariabilityPanel({ data }: { data: VariabilityData }) {
  if (data.status === 'unavailable') {
    return <section className="panel optimization-advice-empty"><div className="panel-heading"><div><h2>优化建议</h2><p>建议只从 Benchmark 合同生成</p></div></div>
      <p className="decision-copy">{data.reason || '该 Benchmark 未声明优化建议规则。'}</p></section>;
  }
  if (!data.groups?.length) {
    return <section className="panel optimization-advice-empty"><div className="panel-heading"><div><h2>优化建议</h2><p>基于成功且通过必要门禁的原始 Benchmark 轮次</p></div></div>
      <p className="decision-copy">暂无有效原始轮次。失败、超时或未通过门禁的证据仍保留，但不会进入统计。</p></section>;
  }
  const recommendations = dedupeRecommendations(data.groups);
  return <div className="variability-report optimization-advice-report">
    <section className="panel optimization-advice-intro">
      <div className="panel-heading"><div><h2><Lightbulb size={17} /> 优化建议</h2><p>只读诊断，不自动修改系统参数</p></div></div>
      <div className="inline-alert neutral"><Info size={16} /><span>当前建议依据 Benchmark 原始数据与合同门禁生成；单目标实验不形成性能优劣、领先或采购结论。</span></div>
      {data.diagnosticContractDigest && <span className="cell-meta">诊断合同：{data.diagnosticContractDigest}</span>}
    </section>
    {data.groups.map(group => <GroupReport key={`${group.targetId}-${group.workloadId}`} group={group} />)}
    <RecommendationSection title="合同诊断与复测建议" items={recommendations} />
    <RecommendationSection title="研究级建议" items={(data.studyRecommendations || []).map(item => ({ ...item, workloads: [] }))} />
  </div>;
}

function GroupReport({ group }: { group: VariabilityGroupReport }) {
  const stats = group.distribution;
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
    {group.stability?.reasons?.length ? <div className="optimization-reasons"><strong>数据事实与诊断原因</strong><ul>{group.stability.reasons.map((reason, index) => <li key={index}><AlertTriangle size={13} />{reason}</li>)}</ul></div> : null}
  </section>;
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
