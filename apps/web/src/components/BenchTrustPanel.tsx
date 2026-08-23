import { Calculator, CircleHelp, Info, Scale } from 'lucide-react';
import { formatNumber } from '../lib/format';
import type {
  BenchTrustData, BenchTrustEnvironmentFactor, BenchTrustEnvironmentSensitivity,
  BenchTrustRankAxis, BenchTrustRankStability, BenchTrustReferenceValidity,
  BenchTrustStatus, BenchTrustTaskLeverage,
} from '../lib/types';

const statusLabels: Record<BenchTrustStatus, string> = {
  available: '已测量', partial: '部分', insufficient_evidence: '证据不足', unavailable: '不可用',
};
const axisLabels: Record<string, string> = {
  machine: '宿主机', day: '日期', scoring_formula: '计分公式',
};
const factorLabels: Record<string, string> = {
  cpu_model: 'CPU 型号', kernel: '内核版本', virtualization: '虚拟化方式', host: '宿主机',
  placement: 'Placement', date: '日期', time_block: '时间块',
};

function StatusTag({ status }: { status: BenchTrustStatus }) {
  return <span className="tag muted">{statusLabels[status] || status}</span>;
}

function pct(value: number | null | undefined, digits = 0): string {
  return value == null ? '—' : `${formatNumber(value * 100, digits)}%`;
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <details className="benchtrust-evidence"><summary><Calculator size={14} /> {title}</summary><div>{children}</div></details>;
}

function Limitations({ items }: { items: string[] }) {
  if (!items?.length) return null;
  return <ul className="benchtrust-limitations">{items.map((item, index) => <li key={index}><CircleHelp size={12} /> {item}</li>)}</ul>;
}

export function BenchTrustPanel({ data }: { data: BenchTrustData }) {
  return <div className="benchtrust-report">
    <section className="panel">
      <div className="panel-heading"><div>
        <h2><Scale size={16} /> Benchmark 可信度</h2>
        <p>四项元指标与性能结果分开，独立展示，不合成一个综合可信度分数。</p>
      </div></div>
      <p className="decision-copy">可信度元指标默认是证据，不作为硬门禁；当前未声明任何阈值，因此只显示「已测量 / 部分 / 证据不足 / 不可用」，不给绿色/黄色/红色评级。</p>
    </section>
    <section className="benchtrust-cards">
      <ReferenceValidityCard value={data.referenceValidityRate} />
      <RankStabilityCard value={data.rankStability} />
      <TaskLeverageCard value={data.taskLeverage} />
      <EnvironmentSensitivityCard value={data.environmentSensitivity} />
    </section>
    <section className="panel">
      <div className="panel-heading"><div><h2>计算证据与可复算性</h2><p>方法版本与摘要</p></div></div>
      <div className="comparison-facts">
        <div><span>合同版本</span><strong>{data.schemaVersion}</strong></div>
        <div><span>方法版本</span><strong>{data.methodVersion}</strong></div>
        <div><span>样本数</span><strong>{data.evidence.sample_count ?? '—'}</strong></div>
        <div><span>目标环境数</span><strong>{data.evidence.target_count ?? '—'}</strong></div>
        <div><span>跨日天数</span><strong>{data.evidence.distinct_dates ?? '—'}</strong></div>
      </div>
      <Limitations items={data.limitations} />
    </section>
  </div>;
}

function Card({ title, plain, status, body, evidence }: {
  title: string; plain: string; status: BenchTrustStatus; body: React.ReactNode; evidence: React.ReactNode;
}) {
  return <section className="panel benchtrust-card">
    <div className="panel-heading"><div><h2>{title}</h2><p>{plain}</p></div><StatusTag status={status} /></div>
    {body}
    <Section title="如何计算 · 计算证据">{evidence}</Section>
  </section>;
}

function ReferenceValidityCard({ value }: { value: BenchTrustReferenceValidity }) {
  const rate = value.rate == null ? '—' : `${formatNumber(value.rate * 100, 0)}%`;
  const coverage = value.eligible_environment_count === 0 ? '无可评价环境' : `${value.valid_environment_count} / ${value.eligible_environment_count} 个可评价环境有效`;
  return <Card
    title="Reference Validity Rate"
    plain="参考配置在多少目标环境中仍能产生稳定、方向一致的信号"
    status={value.status}
    body={<>
      <div className="comparison-facts">
        <div><span>原始值</span><strong>{rate}</strong></div>
        <div><span>有效 / 可评价 / 排除</span><strong>{value.valid_environment_count} / {value.eligible_environment_count} / {value.excluded_environment_count}</strong></div>
        <div><span>置信区间</span><strong>{value.confidence_interval ? `${pct(value.confidence_interval[0])} ~ ${pct(value.confidence_interval[1])}` : '—'}</strong></div>
        <div><span>声明方向</span><strong>{value.expected_direction === 'maximize' ? '越高越好' : '越低越好'}</strong></div>
      </div>
      <p className="cell-meta">覆盖：{coverage}；最小效果判定 {pct(value.minimum_effect)}。</p>
    </>}
    evidence={<>
      <p className="cell-meta">{value.method}（单位：目标环境，不是候选数量）。</p>
      <ul className="benchtrust-criteria">{value.criteria.map((item, index) => <li key={index}>{item}</li>)}</ul>
      {value.environment_results.length ? <div className="table-wrap"><table><thead><tr><th>环境</th><th>有效</th><th>重复</th><th>收益 (CI)</th><th>排除/失败原因</th></tr></thead><tbody>{value.environment_results.map(env => <tr key={env.environment_id}>
        <td>{env.environment_id}</td>
        <td>{env.eligible ? (env.valid ? '是' : '否') : '排除'}</td>
        <td>{env.repeat_count ?? '—'}</td>
        <td className="metric-cell">{env.benefit == null ? '—' : `${pct(env.benefit, 1)}`}{env.benefit_lower != null && env.benefit_upper != null ? ` (${pct(env.benefit_lower, 1)} ~ ${pct(env.benefit_upper, 1)})` : ''}</td>
        <td>{env.excluded_reason ?? env.invalid_reason ?? '—'}</td>
      </tr>)}</tbody></table></div> : null}
      <Limitations items={value.limitations} />
    </>}
  />;
}

function RankStabilityCard({ value }: { value: BenchTrustRankStability }) {
  return <Card
    title="Rank Stability"
    plain="候选排序在跨宿主机、跨日和跨计分公式下是否稳定"
    status={value.status}
    body={<>
      <div className="comparison-facts">
        <div><span>环境轴数</span><strong>{value.axes.length}</strong></div>
        <div><span>可比较轴数</span><strong>{value.axes.filter(axis => axis.comparison_count > 0).length}</strong></div>
        <div><span>方法</span><strong>Kendall τ-b</strong></div>
      </div>
      <AxisTable axes={value.axes} />
    </>}
    evidence={<>
      <p className="cell-meta">仅同 Benchmark、版本、workload 与候选集合下的排名才比较；跨计分公式时列出公式标识。并列名次用 τ-b 处理，不用候选 ID 强行打破平行。</p>
      <Limitations items={value.limitations} />
    </>}
  />;
}

function AxisTable({ axes }: { axes: BenchTrustRankAxis[] }) {
  return <div className="table-wrap"><table><thead><tr><th>环境轴</th><th>切片数</th><th>中位 τ</th><th>最小 τ</th><th>最大 τ</th><th>翻转率</th></tr></thead><tbody>{axes.map(axis => <tr key={axis.axis}>
    <td><strong>{axisLabels[axis.axis] || axis.axis}</strong>{axis.scoring_formula_ids?.length ? <span className="cell-meta"> · {axis.scoring_formula_ids.join(', ')}</span> : null}</td>
    <td>{axis.slice_count}</td>
    <td className="metric-cell">{axis.median_tau == null ? '—' : formatNumber(axis.median_tau, 3)}</td>
    <td className="metric-cell">{axis.minimum_tau == null ? '—' : formatNumber(axis.minimum_tau, 3)}</td>
    <td className="metric-cell">{axis.maximum_tau == null ? '—' : formatNumber(axis.maximum_tau, 3)}</td>
    <td className="metric-cell">{axis.pairwise_flip_rate == null ? '—' : pct(axis.pairwise_flip_rate)}</td>
  </tr>)}</tbody></table></div>;
}

function TaskLeverageCard({ value }: { value: BenchTrustTaskLeverage }) {
  const share = value.maximum_contribution_share == null ? '—' : pct(value.maximum_contribution_share);
  return <Card
    title="Task Leverage"
    plain="单个任务对总分的最大贡献占比"
    status={value.status}
    body={<>
      <div className="comparison-facts">
        <div><span>最大贡献占比</span><strong>{share}</strong></div>
        <div><span>主导任务</span><strong>{value.dominant_task ?? '—'}</strong></div>
        <div><span>留一法最大排名位移</span><strong>{value.leave_one_out.maximum_rank_shift ?? '—'}</strong></div>
        <div><span>移除后冠军变更</span><strong>{value.leave_one_out.winner_changed == null ? '—' : value.leave_one_out.winner_changed ? '是' : '否'}</strong></div>
      </div>
      {value.top_contributors.length ? <div className="table-wrap"><table><thead><tr><th>任务</th><th>权重</th><th>绝对贡献</th><th>贡献占比</th></tr></thead><tbody>{value.top_contributors.map(item => <tr key={item.task_id}>
        <td><strong>{item.task_id}</strong></td>
        <td className="metric-cell">{formatNumber(item.weight, 3)}</td>
        <td className="metric-cell">{formatNumber(item.contribution, 3)}</td>
        <td className="metric-cell">{pct(item.contribution_share)}</td>
      </tr>)}</tbody></table></div> : null}
    </>}
    evidence={<>
      <p className="cell-meta">公式：maxTaskShare = max(Σ|权重⋅贡献|) / Σ|全部任务贡献|；正负贡献用绝对贡献占比。计分公式：{value.scoring_formula ?? '—'}；聚合：{value.aggregation_method ?? '—'}。</p>
      <Limitations items={value.limitations} />
    </>}
  />;
}

function EnvironmentSensitivityCard({ value }: { value: BenchTrustEnvironmentSensitivity }) {
  return <Card
    title="Environment Sensitivity"
    plain="结果有多少与环境因素变化相关"
    status={value.status}
    body={<>
      <p className="benchtrust-association"><Info size={14} /> 统计关联，不代表因果关系。</p>
      <div className="comparison-facts">
        <div><span>关联解释比例（联合）</span><strong>{value.total_explained_ratio == null ? '—' : pct(value.total_explained_ratio)}</strong></div>
        <div><span>残差比例</span><strong>{value.residual_ratio == null ? '—' : pct(value.residual_ratio)}</strong></div>
        <div><span>分析单位</span><strong>{value.analysis_unit}</strong></div>
      </div>
      <FactorTable factors={value.factors} />
      {value.warnings?.length ? <div className="benchtrust-warnings">{value.warnings.map((warning, index) => <p key={index} className="cell-meta">{warning}</p>)}</div> : null}
    </>}
    evidence={<>
      <p className="cell-meta">{value.method}。{value.controls?.length ? `已控制：${value.controls.join('、')}。` : ''}单因素 η² 之间可能重叠，不相加为总解释率。</p>
      <Limitations items={value.limitations} />
    </>}
  />;
}

function FactorTable({ factors }: { factors: BenchTrustEnvironmentFactor[] }) {
  if (!factors.length) return null;
  return <div className="table-wrap"><table><thead><tr><th>环境因素</th><th>组数</th><th>样本</th><th>关联解释比例 (η²)</th><th>缺失率</th></tr></thead><tbody>{factors.map(factor => <tr key={factor.factor}>
    <td><strong>{factorLabels[factor.factor] || factor.factor}</strong></td>
    <td>{factor.group_count}</td>
    <td>{factor.sample_count}</td>
    <td className="metric-cell">{factor.associated_variance_ratio == null ? '—' : pct(factor.associated_variance_ratio)}{factor.confidence_interval ? ` (${pct(factor.confidence_interval[0])} ~ ${pct(factor.confidence_interval[1])})` : ''}</td>
    <td className="metric-cell">{pct(factor.missing_rate)}</td>
  </tr>)}</tbody></table></div>;
}