import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, ExternalLink, FileCode2, FileText, RotateCcw } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { ExperimentActions, type ExperimentAction } from '../components/ActionButtons';
import { BackLink } from '../components/Layout';
import { StatusBadge } from '../components/StatusBadge';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { API_BASE, api } from '../lib/api';
import { formatDate, formatNumber, scoreDelta } from '../lib/format';
import type { AnalysisData, Evaluation, Experiment, SelectionComparison, SelectionTargetResult } from '../lib/types';

const selectionTabs = [['overview', '概览'], ['targets', '目标结果'], ['comparison', '对比结论'], ['evidence', '证据'], ['config', '配置']];
const optimizationTabs = [['overview', '概览'], ['evaluations', '评估记录'], ['pareto', 'Pareto 前沿'], ['evidence', '证据'], ['config', '配置']];

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
    enabled: Boolean(id) && ['targets', 'comparison', 'pareto', 'evidence'].includes(tab),
  });
  const action = useMutation({
    mutationFn: (value: ExperimentAction) => api.experimentAction(id, value),
    onSuccess: data => queryClient.setQueryData(['experiment', id], data),
  });
  const retry = useMutation({
    mutationFn: (attemptId: string) => api.retryAttempt(attemptId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['experiment', id] }),
  });
  if (query.isLoading) return <div className="page"><LoadingState /></div>;
  if (query.isError || !query.data) return <div className="page"><BackLink to="/experiments">返回选型研究</BackLink><ErrorState error={query.error} onRetry={() => query.refetch()} /></div>;

  const experiment = query.data;
  const tabs = selectionMode ? selectionTabs : optimizationTabs;
  const delta = scoreDelta(experiment.bestScore, experiment.baselineScore);
  return <div className="page">
    <BackLink to="/experiments">返回选型研究</BackLink>
    <header className="detail-heading"><div>
      <div className="title-line"><h1>{experiment.name || `研究 ${experiment.id}`}</h1><StatusBadge status={experiment.status} /></div>
      <p>{experiment.decisionQuestion || experiment.description || '暂无研究描述。'}</p>
      <span className="id-label">ID: {experiment.id} · 更新于 {formatDate(experiment.updatedAt)}</span>
    </div><ExperimentActions status={experiment.status} busy={action.isPending} onAction={value => action.mutate(value)} /></header>
    {action.isError && <div className="inline-alert"><AlertTriangle size={16} />{action.error.message}</div>}
    <nav className="tabs" aria-label="研究详情">{tabs.map(([key, label]) => <button key={key} className={tab === key ? 'active' : ''} onClick={() => setTab(key)}>{label}</button>)}</nav>
    {tab === 'overview' && <Overview experiment={experiment} evaluations={experiment.evaluations || []} delta={delta} />}
    {tab === 'evaluations' && <Evaluations items={experiment.evaluations || []} retrying={retry.isPending} onRetry={attemptId => retry.mutate(attemptId)} />}
    {tab === 'targets' && <AsyncPanel query={analysis}><TargetResults items={analysis.data?.targets || []} /></AsyncPanel>}
    {tab === 'comparison' && <AsyncPanel query={analysis}><Comparisons items={analysis.data?.comparisons || []} /></AsyncPanel>}
    {tab === 'pareto' && <AsyncPanel query={analysis}><Pareto data={analysis.data?.pareto || []} /></AsyncPanel>}
    {tab === 'evidence' && <AsyncPanel query={analysis}><Evidence items={analysis.data?.evidence || []} /></AsyncPanel>}
    {tab === 'config' && <Config value={experiment.config || {}} />}
  </div>;
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
  return <section className="panel table-panel"><div className="table-wrap"><table><thead><tr><th>候选</th><th>状态</th><th>得分</th><th>耗时</th><th>指标</th><th>操作</th></tr></thead><tbody>{items.map(item => <tr key={item.id}><td>{item.candidate}</td><td><StatusBadge status={item.status} /></td><td className="metric-cell">{formatNumber(item.score)}</td><td>{item.duration == null ? '—' : `${formatNumber(item.duration, 1)}s`}</td><td>{item.metrics?.map(metric => `${metric.name}=${formatNumber(metric.value)}`).join(' · ') || '—'}</td><td>{item.attemptId && item.status === 'failed' ? <button className="icon-button" title="重试" aria-label={`重试 ${item.candidate}`} disabled={retrying} onClick={() => onRetry(item.attemptId!)}><RotateCcw size={15} /></button> : '—'}</td></tr>)}</tbody></table></div></section>;
}

function Pareto({ data }: { data: AnalysisData['pareto'] }) {
  if (!data?.length) return <EmptyState title="暂无 Pareto 数据" />;
  return <section className="panel table-panel"><div className="table-wrap"><table><thead><tr><th>候选</th><th>得分</th><th>成本</th><th>延迟</th></tr></thead><tbody>{data.map((item, index) => <tr key={item.id || index}><td>{item.candidate}</td><td>{formatNumber(item.score)}</td><td>{formatNumber(item.cost)}</td><td>{formatNumber(item.latency)}</td></tr>)}</tbody></table></div></section>;
}

function Evidence({ items }: { items: NonNullable<AnalysisData['evidence']> }) {
  if (!items.length) return <EmptyState title="暂无证据" />;
  return <div className="evidence-list">{items.map(item => <article key={item.id}><div className="evidence-icon">{item.kind === 'config' ? <FileCode2 size={19} /> : <FileText size={19} />}</div><div><div className="catalog-title"><h2>{item.title}</h2>{item.kind && <span className="tag">{item.kind}</span>}</div><p>{item.summary || '无摘要'}</p><span className="cell-meta">{formatDate(item.createdAt)}</span></div><ArtifactLinks items={item.artifacts || []} /></article>)}</div>;
}

function ArtifactLinks({ items }: { items: Array<{ name: string; url: string }> }) {
  if (!items.length) return <>—</>;
  return <div className="artifact-links">{items.map((item, index) => {
    const href = item.url.startsWith('http') ? item.url : `${new URL(API_BASE).origin}${item.url}`;
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
