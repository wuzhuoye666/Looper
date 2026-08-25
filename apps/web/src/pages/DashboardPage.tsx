import { useQuery } from '@tanstack/react-query';
import { ArrowRight, CheckCircle2, Clock3, Cpu, GitCompareArrows, Plus, TrendingUp } from 'lucide-react';
import { Link } from 'react-router-dom';
import { ExperimentTable } from '../components/ExperimentTable';
import { ScenarioComparisonPanel } from '../components/ScenarioComparisonPanel';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';
import { formatNumber } from '../lib/format';

export function DashboardPage() {
  const query = useQuery({ queryKey: ['dashboard'], queryFn: api.dashboard, refetchInterval: 15_000 });
  if (query.isLoading) return <div className="page"><LoadingState label="正在汇总运行数据" /></div>;
  if (query.isError) return <div className="page"><ErrorState error={query.error} onRetry={() => query.refetch()} /></div>;
  const d = query.data || {};
  const experiments = d.activeExperiments?.length ? d.activeExperiments : d.recentExperiments || [];
  const stats = [
    { label: '研究总数', value: d.totalExperiments ?? Object.values(d.counts || {}).reduce((a, b) => a + (b || 0), 0), icon: GitCompareArrows },
    { label: '执行中', value: d.counts?.running ?? d.activeExperiments?.length ?? 0, icon: Clock3 },
    { label: '完成率', value: d.successRate == null ? '—' : `${formatNumber(d.successRate <= 1 ? d.successRate * 100 : d.successRate, 1)}%`, icon: CheckCircle2 },
    { label: '证据时长', value: d.computeHours == null ? '—' : `${formatNumber(d.computeHours, 1)} h`, icon: Cpu },
  ];
  return <div className="page">
    <header className="workspace-heading"><div><p className="eyebrow">服务器采购</p><h1>选型总览</h1><p>跟踪场景覆盖、候选资源和可复核的比较证据。</p></div><Link className="button primary" to="/experiments/new"><Plus size={16} />新建选型研究</Link></header>
    <section className="stat-grid" aria-label="关键指标">{stats.map(({ label, value, icon: Icon }) => <div className="stat-block" key={label}><div className="stat-label"><span>{label}</span><Icon size={17} /></div><strong>{value}</strong></div>)}</section>
    <section className="content-grid dashboard-grid"><div className="panel chart-panel"><div className="panel-heading"><div><h2>同场景能力对比</h2><p>同一场景、同一基准版本 · 场景内最佳 = 100</p></div><span className="trend-note"><TrendingUp size={14} />15 秒自动刷新</span></div><ScenarioComparisonPanel comparisons={d.scenarioComparisons} /></div>
    <div className="panel run-summary"><div className="panel-heading"><div><h2>研究状态</h2><p>选型证据生命周期</p></div></div>{Object.entries(d.counts || {}).length ? <div className="distribution">{Object.entries(d.counts || {}).map(([key, value]) => <div key={key}><div><span>{({draft:'草稿',queued:'排队',running:'运行中',paused:'已暂停',completed:'已完成',failed:'失败',cancelled:'已取消'} as Record<string,string>)[key] || key}</span><strong>{value}</strong></div><div className="bar"><span className={`bar-${key}`} style={{ width: `${Math.max(4, ((value || 0) / Math.max(1, d.totalExperiments || 1)) * 100)}%` }} /></div></div>)}</div> : <EmptyState title="暂无选型研究" description="先定义采购问题、场景与候选资源。" />}</div></section>
    <section className="panel table-panel"><div className="panel-heading"><div><h2>近期研究</h2><p>按最近更新时间排序</p></div><Link className="text-link" to="/experiments">查看全部<ArrowRight size={15}/></Link></div>{experiments.length ? <ExperimentTable experiments={experiments.slice(0, 6)} /> : <EmptyState title="没有近期研究" action={<Link className="button primary" to="/experiments/new">新建选型研究</Link>} />}</section>
  </div>;
}
