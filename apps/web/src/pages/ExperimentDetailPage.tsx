import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CircleHelp, ExternalLink, FileCode2, FileText, LoaderCircle, RotateCcw, ShieldCheck, WandSparkles } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ExperimentActions, type ExperimentAction } from '../components/ActionButtons';
import { BackLink } from '../components/Layout';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { ExperimentTerminal } from '../components/ExperimentTerminal';
import { API_BASE, api, resolveApiUrl } from '../lib/api';
import { formatDate, formatNumber, scoreDelta } from '../lib/format';
import type { AnalysisData, BenchmarkResultSection, Evaluation, Experiment, MetricDefinition, PostOptimizationStatus, SelectionComparison, SelectionTargetResult } from '../lib/types';

const executionPhases = [
  ['deploying-package', '下发脚本'],
  ['checking-environment', '检查环境'],
  ['preparing-environment', '安装依赖'],
  ['warming-up', '预热'],
  ['running-benchmark', '执行测试'],
  ['normalizing-results', '整理结果'],
  ['validating-results', '校验结果'],
  ['collecting-evidence', '收集证据'],
  ['cleaning-up', '清理环境'],
  ['uploading-evidence', '回传证据'],
];

export function ExperimentDetailPage() {
  const { id = '' } = useParams();
  const [tab, setTab] = useState('overview');
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['experiment', id],
    queryFn: () => api.experiment(id),
    enabled: Boolean(id),
    refetchInterval: current => ['running', 'queued'].includes(current.state.data?.status || '') ? 5000 : 15000,
  });
  const selectionMode = query.data?.mode === 'selection';
  const analysis = useQuery({
    queryKey: ['analysis', id],
    queryFn: () => api.analysis(id),
    enabled: Boolean(id) && ['results', 'evidence'].includes(tab),
  });
  const postOptimization = useQuery({
    queryKey: ['post-optimization', id],
    queryFn: () => api.postOptimization(id),
    enabled: Boolean(id) && query.data?.status === 'completed' && query.data?.mode !== 'selection',
    refetchInterval: current => current.state.data?.status === 'retesting' ? 5000 : false,
  });
  const action = useMutation({
    mutationFn: (value: ExperimentAction) => api.experimentAction(id, value),
    onSuccess: data => queryClient.setQueryData(['experiment', id], data),
  });
  const retry = useMutation({
    mutationFn: (attemptId: string) => api.retryAttempt(attemptId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['experiment', id] }),
  });
  const optimize = useMutation({
    mutationFn: () => api.startPostOptimization(id),
    onSuccess: data => queryClient.setQueryData(['post-optimization', id], data),
  });
  if (query.isLoading) return <div className="page"><LoadingState /></div>;
  if (query.isError || !query.data) return <div className="page"><BackLink to="/experiments">返回选型研究</BackLink><ErrorState error={query.error} onRetry={() => query.refetch()} /></div>;

  const experiment = query.data;
  const tabs = experimentTabs(experiment);
  const delta = scoreDelta(experiment.bestScore, experiment.baselineScore);
  return <div className="page">
    <BackLink to="/experiments">返回选型研究</BackLink>
    <header className="detail-heading"><div>
      <div className="title-line"><h1>{experiment.name || `研究 ${experiment.id}`}</h1><StatusBadge status={experiment.status} /></div>
      <p>{experiment.decisionQuestion || experiment.description || '暂无研究描述。'}</p>
      <span className="id-label">ID: {experiment.id} · 更新于 {formatDate(experiment.updatedAt)}</span>
    </div><ExperimentActions status={experiment.status} busy={action.isPending} onAction={value => action.mutate(value)} /></header>
    {action.isError && <div className="inline-alert"><AlertTriangle size={16} />{action.error.message}</div>}
    {experiment.activePhase && ['queued', 'running'].includes(experiment.status) && <section className="panel execution-progress" aria-label="自动部署与测试进度"><div className="execution-progress-heading"><LoaderCircle size={19}/><div><small>Looper 自动执行中</small><strong>{experiment.activePhaseDetail || experiment.activePhase}</strong></div></div><ol>{executionPhases.map(([key,label],index)=>{const activeIndex=executionPhases.findIndex(([phase])=>phase===experiment.activePhase);return <li className={key===experiment.activePhase?'active':index<activeIndex?'done':''} key={key}><span>{index+1}</span><small>{label}</small></li>;})}</ol></section>}
     {experiment.status === 'completed' && !selectionMode && <PostOptimizationPanel
      data={postOptimization.data}
      loading={postOptimization.isLoading}
      error={postOptimization.isError ? postOptimization.error : optimize.isError ? optimize.error : null}
      busy={optimize.isPending}
      onStart={() => optimize.mutate()}
    />}
    <nav className="tabs" aria-label="研究详情">{tabs.map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}</button>)}</nav>
    {tab === 'overview' && <Overview experiment={experiment} evaluations={experiment.evaluations || []} delta={delta} />}
    {tab === 'results' && (selectionMode
      ? <AsyncPanel query={analysis}><TargetResults items={analysis.data?.targets || []} /></AsyncPanel>
      : <Evaluations items={experiment.evaluations || []} retrying={retry.isPending} onRetry={attemptId => retry.mutate(attemptId)} />)}
    {tab.startsWith('benchmark:') && <BenchmarkMetricSection
      section={(experiment.resultSections || []).find(item => `benchmark:${item.id}` === tab)}
      definitions={experiment.metricDefinitions}
      evaluations={experiment.evaluations || []}
    />}
    {tab === 'evidence' && <AsyncPanel query={analysis}><Evidence items={analysis.data?.evidence || []} /></AsyncPanel>}
    {tab === 'config' && <Config value={experiment.config || {}} />}
     {tab === 'terminal' && <ExperimentTerminal experimentId={experiment.id} />}
  </div>;
}

function PostOptimizationPanel({ data, loading, error, busy, onStart }: {
  data?: PostOptimizationStatus; loading: boolean; error: unknown; busy: boolean; onStart: () => void;
}) {
  if (loading) return <section className="panel post-optimization-panel"><LoadingState /></section>;
  if (error) return <section className="panel post-optimization-panel"><ErrorState error={error} /></section>;
  if (!data) return null;
  const statusLabels: Record<PostOptimizationStatus['status'], string> = {
    ready: '可以优化', retesting: '正在复测', accepted: '建议保留', rolled_back: '保留原配置',
    inconclusive: '证据不足', unavailable: '没有安全动作', failed: '流程失败',
  };
  const icon = data.status === 'accepted'
    ? <ShieldCheck size={19} />
    : data.status === 'retesting'
      ? <LoaderCircle size={19} />
      : data.status === 'rolled_back'
        ? <RotateCcw size={19} />
        : data.status === 'inconclusive'
          ? <CircleHelp size={19} />
          : <WandSparkles size={19} />;
  const before = data.action?.before == null ? '—' : String(data.action.before);
  const after = data.action?.after == null ? '—' : String(data.action.after);
  return <section className={`panel post-optimization-panel ${data.status}`}>
    <div className="post-optimization-icon">{icon}</div>
    <div className="post-optimization-copy">
      <div className="catalog-title"><h2>Benchmark 完成后的优化复测</h2><span className="tag">{statusLabels[data.status]}</span></div>
      <p>{data.reason}</p>
      {data.action && <div className="post-optimization-action">
        <strong>{data.action.label}</strong>
        <span>{data.action.parameter}: {before} → {after}</span>
        <small>{data.action.description || '只执行 Benchmark 声明的低风险白名单动作。'}</small>
      </div>}
    </div>
    <div className="post-optimization-buttons">
      {data.status === 'ready' && <button className="button primary" disabled={busy} onClick={onStart}>
        <WandSparkles size={15} />{busy ? '正在创建复测…' : '优化并重新测试'}
      </button>}
      {data.followUpExperiment && <Link className="button secondary" to={`/experiments/${data.followUpExperiment.id}`}>
        查看复测实验
      </Link>}
    </div>
  </section>;
}

function AsyncPanel({ query, children }: { query: { isLoading: boolean; isError: boolean; error: unknown; refetch: () => unknown }; children: React.ReactNode }) {
  if (query.isLoading) return <LoadingState />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  return <>{children}</>;
}

function Overview({ experiment, evaluations, delta }: { experiment: Experiment; evaluations: Evaluation[]; delta?: number }) {
  if (experiment.mode === 'selection') {
    const strength = strengthLabel(experiment.comparison?.conclusion_strength);
    const stats = [
      { label: '候选资源', value: experiment.targetIds?.length ?? experiment.targetNames?.length ?? 0, note: experiment.targetNames?.join(' · ') || '—' },
      { label: '场景主指标', value: experiment.objective || '—', note: experiment.benchmarkName || '—' },
      { label: '证据进度', value: `${Math.round(experiment.progress || 0)}%`, note: `${experiment.attempts || 0} / ${experiment.maxAttempts || 0} attempts` },
      { label: '结论强度', value: strength, note: experiment.comparison?.winner ? `当前领先：${experiment.comparison.winner}` : '尚无可区分结论' },
    ];
    return <>
      <section className="stat-grid detail-stats">{stats.map(item => <div className="stat-block" key={item.label}><div className="stat-label"><span>{item.label}</span></div><strong>{item.value}</strong><small>{item.note}</small></div>)}</section>
      <section className="content-grid overview-grid">
        <div className="panel"><div className="panel-heading"><div><h2>采购问题</h2><p>{experiment.scenario?.workload_class || 'scenario'}</p></div></div><div className="decision-copy">{experiment.decisionQuestion || experiment.scenario?.decision_question || experiment.description}</div></div>
        <div className="panel"><div className="panel-heading"><div><h2>场景边界</h2></div></div><dl className="info-list"><div><dt>拓扑</dt><dd>{experiment.scenario?.topology || '—'}</dd></div><div><dt>主指标</dt><dd>{experiment.scenario?.primary_metric || experiment.objective || '—'}</dd></div><div><dt>重复</dt><dd>{evaluations.length ? `${evaluations.length} evaluations` : '待执行'}</dd></div></dl></div>
      </section>
    </>;
  }
  const stats = [
    { label: '最佳得分', value: formatNumber(experiment.bestScore), note: delta == null ? '暂无基线' : `${delta >= 0 ? '+' : ''}${formatNumber(delta, 1)}% 对比基线` },
    { label: '已评估', value: `${experiment.attempts ?? evaluations.length} / ${experiment.maxAttempts ?? '—'}`, note: `完成 ${Math.round(experiment.progress ?? 0)}%` },
    { label: '目标', value: experiment.targetName || experiment.targetId || '—', note: experiment.benchmarkName || experiment.benchmarkId || '—' },
    { label: '状态', value: experiment.status, note: formatDate(experiment.updatedAt) },
  ];
  return <section className="stat-grid detail-stats">{stats.map(item => <div className="stat-block" key={item.label}><div className="stat-label"><span>{item.label}</span></div><strong>{item.value}</strong><small>{item.note}</small></div>)}</section>;
}

export function experimentTabs(experiment: Experiment): string[][] {
  const benchmarkTabs = (experiment.resultSections || []).slice(0, 2).map(
    section => [`benchmark:${section.id}`, section.label],
  );
  return [
    ['overview', '概览'],
    ['results', experiment.mode === 'selection' ? '目标结果' : '运行结果'],
    ...benchmarkTabs,
    ['evidence', '证据'],
    ['config', '配置'],
    ['terminal', '原始终端'],
  ];
}

function BenchmarkMetricSection({ section, definitions, evaluations }: {
  section?: BenchmarkResultSection;
  definitions?: Record<string, MetricDefinition>;
  evaluations: Evaluation[];
}) {
  if (!section) return <EmptyState title="该 Benchmark 未声明此结果栏目" />;
  if (section.view === 'sysbench-workloads') {
    return <SysbenchWorkloadSection section={section} definitions={definitions} evaluations={evaluations} />;
  }
  return <section className="panel metric-definitions">
    <div className="panel-heading"><div><h2>{section.label}</h2><p>{section.description || '由 Benchmark manifest 声明的专属结果。'}</p></div></div>
    <div className="metric-definition-grid">{section.metrics.map(name => {
      const definition = definitions?.[name];
      const presentation = definition?.presentation;
      const samples = evaluations.flatMap(item => item.metrics || []).filter(metric => metric.name === name);
      const latest = samples[0];
      return <article key={name}>
        <div className="metric-definition-title"><strong>{presentation?.userLabel || name}</strong><span className="tag">{name}</span></div>
        <strong className="metric-cell">{latest ? `${formatNumber(latest.value)} ${latest.unit || definition?.unit || ''}`.trim() : '待测试'}</strong>
        <p>{presentation?.userDescription || definition?.description || '暂无指标说明。'}</p>
        <span className="cell-meta">{samples.length ? `${samples.length} 个已采集样本` : '尚未采集该指标'}</span>
      </article>;
    })}</div>
  </section>;
}

const sysbenchMetricColumns = [
  ['events_per_sec', '每秒事件数'],
  ['throughput_mib_s', '内存吞吐量'],
  ['latency_avg_ms', '平均延迟'],
  ['latency_p95_ms', 'P95 延迟'],
  ['latency_max_ms', '最大延迟'],
] as const;

export function SysbenchWorkloadSection({ section, definitions, evaluations }: {
  section: BenchmarkResultSection;
  definitions?: Record<string, MetricDefinition>;
  evaluations: Evaluation[];
}) {
  const value = (evaluation: Evaluation, name: string) => {
    const metric = evaluation.metrics?.find(item => item.name === name);
    if (!metric) return '—';
    const precision = definitions?.[name]?.presentation?.displayPrecision ?? 2;
    return `${formatNumber(metric.value, precision)} ${metric.unit || definitions?.[name]?.unit || ''}`.trim();
  };
  const workloadNames: Record<string, string> = { cpu: 'CPU 素数计算', memory: '内存吞吐', thread: '线程调度', mutex: '互斥锁竞争' };
  return <section className="panel metric-definitions sysbench-results">
    <div className="panel-heading"><div><h2>{section.label}</h2><p>{section.description || '由 Benchmark manifest 声明的专属结果。'}</p></div></div>
    {evaluations.length ? <div className="metric-definition-grid sysbench-workload-grid">{evaluations.map(item => {
      const primaryMetric = item.workload === 'memory' ? 'throughput_mib_s' : 'events_per_sec';
      const primaryDefinition = definitions?.[primaryMetric];
      const secondaryMetrics = sysbenchMetricColumns.filter(([name]) => name !== primaryMetric && item.metrics?.some(metric => metric.name === name));
      const measured = item.metrics?.some(metric => metric.name !== 'sysbench_run_ok');
      return <article key={item.id}>
        <div className="metric-definition-title"><strong>{workloadNames[item.workload || ''] || item.workload || '未命名 workload'}</strong><StatusBadge status={item.status} /></div>
        <span className="cell-meta">{item.candidate}</span>
        <div className="sysbench-primary-metric"><span>{primaryDefinition?.presentation?.userLabel || (primaryMetric === 'throughput_mib_s' ? '内存吞吐量' : '每秒事件数')}</span><strong className="metric-cell">{value(item, primaryMetric)}</strong></div>
        {secondaryMetrics.length > 0 && <dl className="sysbench-secondary-metrics">{secondaryMetrics.map(([name, fallback]) => <div key={name}><dt>{definitions?.[name]?.presentation?.userLabel || fallback}</dt><dd>{value(item, name)}</dd></div>)}</dl>}
        <p>{item.phaseDetail || (measured ? '已完成指标回传。' : '本次运行未形成有效指标。')}</p>
        <span className={measured ? 'cell-meta' : 'cell-error'}>{measured ? '已回传指标与原始证据' : '未形成有效指标'}</span>
      </article>;
    })}</div> : <EmptyState title="暂无 Sysbench workload 数据" description="完成至少一个 workload 后，这里会显示专属指标。" />}
    <div className="sysbench-result-note"><strong>数据口径</strong><span>每张卡展示该 workload 最新一次带观测值的结果；原始 stdout、raw-result.json 和系统指纹仍在“证据”与“原始终端”中保留。</span></div>
  </section>;
}

function MetricDefinitions({ definitions }: { definitions?: Record<string, MetricDefinition> }) {
  const entries = Object.entries(definitions || {}).filter(([, definition]) => definition.presentation?.defaultVisibility !== 'hidden');
  if (!entries.length) return null;
  const roleLabels: Record<string, string> = { primary_outcome: '主结果', hard_gate: '硬门槛', guardrail: '护栏', cost_efficiency: '成本效率', stability: '稳定性', diagnostic: '诊断', context: '上下文' };
  return <section className="panel metric-definitions"><div className="panel-heading"><div><h2>声明指标</h2><p>来自 Benchmark manifest 的 suite-specific presentation</p></div></div><div className="metric-definition-grid">{entries.map(([name, definition]) => {
    const presentation = definition.presentation;
    const label = presentation?.userLabel || name;
    const roles = presentation?.roles?.map(role => roleLabels[role] || role).join(' · ');
    const details = [roles, definition.unit, definition.direction && (definition.direction === 'maximize' ? '越高越好' : definition.direction === 'minimize' ? '越低越好' : '方向无关')].filter(Boolean).join(' · ');
    return <article key={name}><div className="metric-definition-title"><strong>{label}</strong><span className="tag">{name}</span></div>{details && <span className="cell-meta">{details}</span>}<p>{presentation?.userDescription || definition.description || '暂无指标说明。'}</p></article>;
  })}</div></section>;
}

function TargetResults({ items }: { items: SelectionTargetResult[] }) {
  if (!items.length) return <EmptyState title="暂无目标结果" description="完成有效 block 后显示每个候选资源的主指标。" />;
  return <section className="panel table-panel"><div className="table-wrap"><table><thead><tr><th>候选资源</th><th>Placement</th><th>有效 / 无效 block</th><th>主指标</th><th>单位价格容量</th><th>状态</th></tr></thead><tbody>{items.map(item => {
    const primary = item.metrics[0];
    return <tr key={item.target_id}><td><strong>{item.label}</strong><span className="cell-meta">{item.variant_id}</span></td><td>{item.placement_pair_id}</td><td>{item.valid_block_count} / {item.invalid_block_count}</td><td className="metric-cell">{formatNumber(primary?.raw)} {primary?.unit}</td><td>{item.price_efficiency ? `${formatNumber(item.price_efficiency.value)} ${item.price_efficiency.unit}` : '—'}</td><td>{item.status}</td></tr>;
  })}</tbody></table></div></section>;
}

function Comparisons({ items }: { items: SelectionComparison[] }) {
  if (!items.length) return <EmptyState title="暂无对比结论" description="至少两个变体与成对 block 才能形成比较。" />;
  return <div className="comparison-list">{items.map(item => <section className="panel comparison-panel" key={`${item.metric}-${item.baseline_variant}-${item.candidate_variant}`}>
    <div className="panel-heading"><div><h2>{item.metric}</h2><p>{item.baseline_variant} vs {item.candidate_variant}</p></div><span className="tag">{strengthLabel(item.conclusion_strength)}</span></div>
    <div className="comparison-facts"><div><span>估计改善</span><strong>{item.estimate == null ? '—' : `${formatNumber(item.estimate * 100, 2)}%`}</strong></div><div><span>95% 区间</span><strong>{item.lower == null || item.upper == null ? '—' : `${formatNumber(item.lower * 100, 2)}% ~ ${formatNumber(item.upper * 100, 2)}%`}</strong></div><div><span>推断单位</span><strong>{item.inference_unit || '—'}</strong></div><div><span>结论</span><strong>{item.winner ? `${item.winner} 可区分` : item.reason || '当前预算不可区分'}</strong></div></div>
  </section>)}</div>;
}

function Evaluations({ items, retrying, onRetry }: { items: Evaluation[]; retrying: boolean; onRetry: (id: string) => void }) {
  if (!items.length) return <EmptyState title="暂无评估记录" />;
  return <section className="panel table-panel"><div className="table-wrap"><table><thead><tr><th>候选</th><th>状态 / 阶段</th><th>得分</th><th>耗时</th><th>指标</th><th>操作</th></tr></thead><tbody>{items.map(item => <tr key={item.id}><td>{item.candidate}</td><td><StatusBadge status={item.status} />{item.phaseDetail&&<span className="cell-meta">{item.phaseDetail}</span>}</td><td className="metric-cell">{formatNumber(item.score)}</td><td>{item.duration == null ? '—' : `${formatNumber(item.duration, 1)}s`}</td><td>{item.metrics?.map(metric => `${metric.name}=${formatNumber(metric.value)}`).join(' · ') || '—'}</td><td>{item.attemptId && item.status === 'failed' ? <button className="icon-button" title="重试" aria-label={`重试 ${item.candidate}`} disabled={retrying} onClick={() => onRetry(item.attemptId!)}><RotateCcw size={15} /></button> : '—'}</td></tr>)}</tbody></table></div></section>;
}

function Pareto({ data }: { data: AnalysisData['pareto'] }) {
  if (!data?.length) return <EmptyState title="暂无 Pareto 数据" />;
  const stabilityKeys = Array.from(new Set(data.flatMap(item => Object.keys(item.objectives || {}).filter(key => key.startsWith('stability:')))));
  const stabilityLabel = (key: string) => `稳定性 ${key.slice('stability:'.length)}`;
  return <section className="panel table-panel"><div className="table-wrap"><table><thead><tr><th>候选</th><th>Pareto 排名</th><th>得分</th><th>成本</th><th>延迟</th>{stabilityKeys.map(key => <th key={key}>{stabilityLabel(key)}</th>)}</tr></thead><tbody>{data.map((item, index) => <tr key={item.id || index}><td>{item.candidate}</td><td>{item.rank ?? '—'}</td><td>{formatNumber(item.score)}</td><td>{formatNumber(item.cost)}</td><td>{formatNumber(item.latency)}</td>{stabilityKeys.map(key => <td key={key} className="metric-cell">{item.objectives?.[key] == null ? '—' : formatNumber(item.objectives[key], 4)}</td>)}</tr>)}</tbody></table></div></section>;
}

function Evidence({ items }: { items: NonNullable<AnalysisData['evidence']> }) {
  if (!items.length) return <EmptyState title="暂无证据" />;
  return <div className="evidence-list">{items.map(item => <article key={item.id}><div className="evidence-icon">{item.kind === 'config' ? <FileCode2 size={19} /> : <FileText size={19} />}</div><div><div className="catalog-title"><h2>{item.title}</h2>{item.kind && <span className="tag">{item.kind}</span>}</div><p>{item.summary || '无摘要'}</p><span className="cell-meta">{formatDate(item.createdAt)}</span></div><ArtifactLinks items={item.artifacts || []} /></article>)}</div>;
}

function ArtifactLinks({ items }: { items: Array<{ name: string; url: string }> }) {
  if (!items.length) return <>—</>;
  return <div className="artifact-links">{items.map((item, index) => {
    const href = item.url.startsWith('http') ? item.url : `${resolveApiUrl(API_BASE).origin}${item.url}`;
    return <a key={`${item.url}-${index}`} href={href} target="_blank" rel="noreferrer" title={item.name}>{item.name}<ExternalLink size={12} /></a>;
  })}</div>;
}

function Config({ value }: { value: Record<string, unknown> }) {
  const text = useMemo(() => JSON.stringify(value, null, 2), [value]);
  return <section className="panel config-panel"><div className="panel-heading"><div><h2>研究配置</h2><p>不可变配置快照</p></div></div><pre>{text}</pre></section>;
}

function strengthLabel(value?: string) {
  const labels: Record<string, string> = {
    'availability-only': '仅可用性',
    'single-placement-provisional': '单 placement 暂定',
    'multi-placement-exploratory': '多 placement 探索',
    'procurement-candidate': '采购建议候选',
  };
  return value ? labels[value] || value : '待形成';
}
