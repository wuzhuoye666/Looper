import { Activity, AlertTriangle, CheckCircle2, Lightbulb, ShieldQuestion, TrendingUp } from 'lucide-react';
import { formatNumber } from '../lib/format';
import type { VariabilityComparison, VariabilityData, VariabilityGroupReport, VariabilityStatus } from '../lib/types';

const statusLabels: Record<VariabilityStatus, string> = {
  stable: '稳定', warning: '有波动', unstable: '不稳定', insufficient_evidence: '样本不足',
};
const statusClass: Record<VariabilityStatus, string> = {
  stable: 'ok', warning: 'warn', unstable: 'bad', insufficient_evidence: 'muted',
};
const priorityLabels: Record<string, string> = { high: '高', medium: '中', low: '低' };
const clueDirectionLabels: Record<string, string> = { elevated_in_slow: '慢运行中升高', reduced_in_slow: '慢运行中降低' };
const dimensionLabels: Record<string, string> = {
  host: '宿主机', placement: 'Placement', date: '日期', time_block: '时间块', environment: '环境/目标', within_run: '运行内',
};

function pct(value?: number | null, digits = 1): string {
  return value == null ? '—' : `${value >= 0 ? '+' : ''}${formatNumber(value * 100, digits)}%`;
}


export function VariabilityPanel({ data }: { data: VariabilityData }) {
  if (!data.groups?.length) {
    return <section className="panel"><div className="panel-heading"><div><h2>波动分析</h2><p>{data.metric} · {data.unit}</p></div></div>
      <p className="decision-copy">暂无可用运行数据：需要成功完成的 attempt 才能进行波动分析。</p></section>;
  }
  return <div className="variability-report">
    {data.groups.map(group => <GroupReport key={group.groupLabel} group={group} />)}
    {data.comparisons?.length ? <ComparisonSection items={data.comparisons} /> : null}
  </div>;
}

function GroupReport({ group }: { group: VariabilityGroupReport }) {
  const stats = group.distribution;
  const rows: Array<[string, string]> = [
    ['样本数', formatNumber(stats.count, 0)],
    ['Mean / Median', `${formatNumber(stats.mean)} / ${formatNumber(stats.median)}`],
    ['Std / CV', `${formatNumber(stats.standardDeviation)} / ${stats.coefficientOfVariation == null ? '—' : formatNumber(stats.coefficientOfVariation, 3)}`],
    ['P95 / P99', `${formatNumber(stats.p95)} / ${formatNumber(stats.p99)}`],
    ['尾部均值 (CVaR95)', formatNumber(stats.tailMean)],
    ['Min / Max', `${formatNumber(stats.minimum)} / ${formatNumber(stats.maximum)}`],
    ['IQR / MAD', `${formatNumber(stats.iqr)} / ${formatNumber(stats.mad)}`],
    ['偏度', formatNumber(stats.skewness, 2)],
  ];
  return <section className="panel variability-panel">
    <div className="panel-heading"><div>
      <h2><Activity size={16} /> {group.groupLabel}</h2>
      <p>{group.metric} · {group.unit} · {group.direction === 'minimize' ? '越低越好' : '越高越好'}</p>
    </div><span className={`tag ${statusClass[group.status]}`}>{statusLabels[group.status]}</span></div>
    <p className="decision-copy">{group.selectionImpact?.summary}</p>
    {group.stability?.reasons?.length ? <ul className="variability-reasons">{group.stability.reasons.map((reason, index) => <li key={index}><AlertTriangle size={13} /> {reason}</li>)}</ul> : null}
    <div className="table-wrap"><table><tbody>
      {rows.map(([label, value]) => <tr key={label}><th>{label}</th><td className="metric-cell">{value}</td></tr>)}
    </tbody></table></div>
    <ModesBlock group={group} />
    <RunsBlock group={group} />
    <CluesBlock group={group} />
    <AttributionBlock group={group} />
    <RecommendationsBlock group={group} />
  </section>;
}

function ModesBlock({ group }: { group: VariabilityGroupReport }) {
  if (!group.modes) return null;
  const { modes } = group;
  return <div className="variability-section">
    <h3><TrendingUp size={14} /> 疑似快/慢双模式</h3>
    <div className="comparison-facts">
      <div><span>Fast Mode</span><strong>{formatNumber(modes.fastMode.center)} {group.unit} × {modes.fastMode.count} 次</strong></div>
      <div><span>Slow Mode</span><strong>{formatNumber(modes.slowMode.center)} {group.unit} × {modes.slowMode.count} 次</strong></div>
      <div><span>截止点</span><strong>{formatNumber(modes.cutoff)} {group.unit}</strong></div>
    </div>
  </div>;
}

function RunsBlock({ group }: { group: VariabilityGroupReport }) {
  if (!group.runs?.length) return null;
  const slowRuns = group.runs.filter(run => run.slow);
  const outliers = [...group.outliers.slow, ...group.outliers.fast];
  return <div className="variability-section">
    <h3>运行分类</h3>
    <div className="comparison-facts">
      <div><span>正常运行</span><strong>{group.runs.length - slowRuns.length} / {group.runs.length}</strong></div>
      <div><span>慢运行（含模式）</span><strong>{slowRuns.length} / {group.runs.length}</strong></div>
      <div><span>IQR 异常</span><strong>{outliers.length}</strong></div>
    </div>
    {slowRuns.length ? <div className="run-strip">{group.runs.map(run => (
      <span key={run.runId} className={`run-dot ${run.slow ? 'slow' : 'normal'}`} title={`${run.runId}: ${formatNumber(run.value)} ${group.unit} (${run.label})`} />
    ))}</div> : null}
  </div>;
}

function CluesBlock({ group }: { group: VariabilityGroupReport }) {
  if (!group.associationClues?.length) {
    return <div className="variability-section"><h3><ShieldQuestion size={14} /> 关联线索</h3>
      <p className="cell-meta">未发现达到阈值的系统指标关联，或该组运行缺少系统指标采集。</p></div>;
  }
  return <div className="variability-section"><h3><ShieldQuestion size={14} /> 慢运行关联线索（非因果结论）</h3>
    <div className="table-wrap"><table><thead><tr><th>系统指标</th><th>相关系数</th><th>变化</th><th>慢/正常均值</th><th>备注</th></tr></thead>
      <tbody>{group.associationClues.map(clue => <tr key={clue.metric}>
        <td><strong>{clue.metric}</strong>{clue.likelyConsequence ? <span className="tag warn">疑似结果指标</span> : null}</td>
        <td className="metric-cell">{formatNumber(clue.correlation, 3)}</td>
        <td>{clueDirectionLabels[clue.direction] || clue.direction}{clue.lift != null ? `（${formatNumber(clue.lift, 2)}×）` : ''}</td>
        <td className="metric-cell">{formatNumber(clue.slowMean)} / {formatNumber(clue.normalMean)}</td>
        <td className="cell-meta">{clue.note}</td>
      </tr>)}</tbody></table></div></div>;
}

function AttributionBlock({ group }: { group: VariabilityGroupReport }) {
  if (!group.attribution?.length) return null;
  return <div className="variability-section"><h3>波动来源（方差占比 η²）</h3>
    <div className="attribution-list">{group.attribution.map(entry => (
      <div className="attribution-row" key={entry.dimension}>
        <span className="attribution-label">{dimensionLabels[entry.dimension] || entry.dimension}{entry.dominant ? ' ⚑ 主导' : ''}</span>
        <div className="attribution-bar"><div className={`attribution-fill ${entry.dominant ? 'dominant' : ''}`} style={{ width: `${Math.min(100, Math.round((entry.etaSquared ?? 0) * 100))}%` }} /></div>
        <span className="metric-cell">{entry.etaSquared == null ? '—' : formatNumber(entry.etaSquared, 2)}</span>
      </div>
    ))}</div></div>;
}

function RecommendationsBlock({ group }: { group: VariabilityGroupReport }) {
  if (!group.recommendations?.length) return null;
  return <div className="variability-section"><h3><Lightbulb size={14} /> 下一步验证实验建议</h3>
    <ul className="recommendation-list">{group.recommendations.map((item, index) => (
      <li key={index} className={`priority-${item.priority}`}>
        <span className="tag">{priorityLabels[item.priority] || item.priority}</span>
        <div><strong>{item.action}</strong><p className="cell-meta">{item.rationale}</p></div>
      </li>
    ))}</ul></div>;
}

function ComparisonSection({ items }: { items: VariabilityComparison[] }) {
  return <section className="panel">
    <div className="panel-heading"><div><h2><CheckCircle2 size={16} /> 分布比较（均值之外）</h2><p>同时比较均值、尾部、稳定性与慢运行概率，避免“均值改善但尾部恶化”被忽略</p></div></div>
    <div className="comparison-list">{items.map((item, index) => <section className="panel comparison-panel" key={index}>
      <div className="panel-heading"><div><h2>{item.metric}</h2><p>{item.baselineLabel} vs {item.candidateLabel}</p></div><span className={`tag ${item.verdict === 'dominant' ? 'ok' : item.verdict === 'dominated' ? 'bad' : 'warn'}`}>{comparisonVerdictLabel(item.verdict)}</span></div>
      <p className="decision-copy">{item.summary}</p>
      <div className="comparison-facts">
        <div><span>均值改善</span><strong>{pct(item.meanImprovement)}</strong></div>
        <div><span>中位数改善</span><strong>{pct(item.medianImprovement)}</strong></div>
        <div><span>尾部改善</span><strong>{pct(item.tailImprovement)}</strong></div>
        <div><span>CV 比</span><strong>{item.cvRatio == null ? '—' : formatNumber(item.cvRatio, 2)}</strong></div>
        <div><span>慢运行概率</span><strong>{formatNumber(item.slowRunProbability.baseline, 2)} → {formatNumber(item.slowRunProbability.candidate, 2)}</strong></div>
        {item.sloExceedance ? <div><span>SLO 超出率</span><strong>{formatNumber(item.sloExceedance.baseline_exceedance, 2)} → {formatNumber(item.sloExceedance.candidate_exceedance, 2)}</strong></div> : null}
      </div>
      <p className="cell-meta comparison-recommendation">{item.recommendation}</p>
    </section>)}</div>
  </section>;
}

function comparisonVerdictLabel(verdict: string): string {
  const labels: Record<string, string> = {
    dominant: '分布占优', dominated: '分布劣势', mean_better_tail_worse: '均值好·尾部差',
    mean_worse_tail_better: '均值差·尾部好', inconclusive: '无显著差异',
  };
  return labels[verdict] || verdict;
}
