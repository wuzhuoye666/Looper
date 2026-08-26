import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Boxes, Cpu, Download, ExternalLink, Filter, LoaderCircle, Plus, RefreshCw, Search, Server, Trash2, TriangleAlert, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ImportTargetDialog } from '../components/ImportTargetDialog';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { TargetStatus } from '../components/StatusBadge';
import { TargetSshButton } from '../components/TargetSshButton';
import { api } from '../lib/api';
import {
  benchmarkDescription, benchmarkMetricLabel, benchmarkMetricLabels, benchmarkName, benchmarkScenario,
  executionBlockerLabel, executionModelLabel,
} from '../lib/benchmarkPresentation';
import { formatDate } from '../lib/format';
import type { Benchmark, Target, TargetDestroyPreview } from '../lib/types';

export function BenchmarksPage() {
  const [search, setSearch] = useState('');
  const query = useQuery({ queryKey: ['benchmarks'], queryFn: api.benchmarks });
  const items = useMemo(() => query.data?.items.filter(item => {
    if (!search) return true;
    const scenario = benchmarkScenario(item);
    const searchable = [
      item.name, benchmarkName(item), item.description, benchmarkDescription(item), scenario.label, scenario.detail,
      item.category, ...(item.tags || []), ...benchmarkMetricLabels(item), ...(item.metrics || []),
    ].filter(Boolean).join(' ').toLowerCase();
    return searchable.includes(search.toLowerCase());
  }) || [], [query.data, search]);
  return <div className="page">
    <PageHeader title="测试场景目录" description="查看测试场景、套件名字、套件内容和相关参数。" actions={<Link className="button primary" to="/benchmarks/register"><Plus size={16}/>注册测试套件</Link>}/>
    <div className="toolbar"><label className="search-field"><Search size={16}/><span className="sr-only">搜索测试套件</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="搜索测试场景、套件名字或内容"/></label><span className="result-count">{items.length} 个测试套件</span></div>
    {query.isLoading ? <LoadingState/> : query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()}/> : items.length ? <div className="catalog-grid">{items.map(item => <BenchmarkCatalogCard benchmark={item} key={item.id}/>)}</div> : <EmptyState title="没有匹配的测试套件"/>}
  </div>;
}

function BenchmarkCatalogCard({ benchmark }: { benchmark: Benchmark }) {
  const scenario = benchmarkScenario(benchmark);
  const metrics = benchmarkMetricLabels(benchmark);
  const blocker = executionBlockerLabel(benchmark);
  return <article className="catalog-item">
    <div className="catalog-icon"><Boxes size={20}/></div>
    <div className="catalog-main">
      <section className="catalog-field catalog-scenario"><span className="catalog-field-label">测试场景</span><div><strong>{scenario.label}</strong><small>{scenario.detail}</small></div></section>
      <section className="catalog-field"><span className="catalog-field-label">套件名字</span><div className="catalog-title"><h2>{benchmarkName(benchmark)}</h2>{benchmark.version && <span className="tag">{benchmark.version}</span>}</div></section>
      <section className="catalog-field catalog-content"><span className="catalog-field-label">套件内容</span><p>{benchmarkDescription(benchmark)}</p></section>
      <section className="catalog-field"><span className="catalog-field-label">相关参数</span><div className="tags">{metrics.length ? metrics.map((label, index) => <span key={`${benchmark.metrics?.[index]}-${label}`}>{label}</span>) : <span>{benchmarkMetricLabel(benchmark, benchmark.primaryMetric)}</span>}</div></section>
      <div className="catalog-meta"><span>执行方式：{executionModelLabel(benchmark.executionModel)}</span><span>{benchmark.selectable === false ? '暂不可选择' : benchmark.runnable ? '可直接测试' : '暂不可执行'}</span><span>{benchmark.cases == null ? '测试项未知' : `${benchmark.cases} 个测试项`}</span><span>更新于 {formatDate(benchmark.updatedAt)}</span></div>
      {blocker && <div className="catalog-blocker"><TriangleAlert size={14}/><span>{blocker}</span></div>}
    </div>
  </article>;
}

const CLOUD_PROVIDERS = new Set(['tencent', 'alibaba', 'volcengine', 'baidu']);

const DESTROY_KIND_LABELS: Record<string, string> = {
  'instance': '实例', 'system-disk': '系统盘', 'local-disk': '本地盘',
  'public-ip': '公网 IP', 'vpc': 'VPC', 'subnet': '子网', 'security-group': '安全组',
};

export function TargetsPage() {
  const [search, setSearch] = useState('');
  const [status, setStatus] = useState('active');
  const [importOpen, setImportOpen] = useState(false);
  const [destroyTarget, setDestroyTarget] = useState<Target | null>(null);
  const [sshTarget, setSshTarget] = useState<Target | null>(null);
  const [cloudSyncing, setCloudSyncing] = useState(false);
  const query = useQuery({
    queryKey: ['targets', 'all'],
    queryFn: () => api.targets(true),
    // The API checkpoints every completed cloud region. Poll while the long
    // cross-region sync is running so newly bought machines appear immediately.
    refetchInterval: cloudSyncing ? 1_500 : 30_000,
  });
  const syncCloud = useMutation({
    mutationFn: api.syncCloudTargets,
    onMutate: () => setCloudSyncing(true),
    onSettled: () => {
      setCloudSyncing(false);
      return query.refetch();
    },
  });
  const items = useMemo(() => query.data?.items.filter(x => {
    if (x.type === 'local' || x.id === 'local') return false;
    const statusMatches = status === 'all'
      || (status === 'active' ? x.lifecycleStatus !== 'missing' && x.lifecycleStatus !== 'archived'
        : status === 'missing' || status === 'archived' ? x.lifecycleStatus === status : x.status === status);
    return statusMatches && (!search || `${x.name} ${x.id} ${x.framework || ''} ${x.hardware || ''}`.toLowerCase().includes(search.toLowerCase()));
  }) || [], [query.data, search, status]);
  const lifecycleLabel = (target: typeof items[number]) =>
    target.lifecycleStatus === 'archived' ? '历史归档'
      : target.lifecycleStatus === 'missing' ? `云端不可见 · 连续 ${target.inventoryMissCount || 1} 次` : '当前活跃';
  return (
    <div className="page">
      <PageHeader title="候选资源" description="查看服务器规格、环境指纹和执行就绪状态。" actions={<>
        <button className="button secondary" disabled={syncCloud.isPending} onClick={() => syncCloud.mutate()}><RefreshCw className={syncCloud.isPending ? 'spin' : undefined} size={15} />{syncCloud.isPending ? '逐地区同步中…' : '同步云库存'}</button>
        <button className="button primary" onClick={() => setImportOpen(true)}><Download size={15} />连接外部机器</button>
      </>} />
      {syncCloud.isError && <div className="inline-alert"><AlertTriangle size={16} />{syncCloud.error.message}</div>}
      {syncCloud.isPending && <div className="inline-alert" role="status"><RefreshCw className="spin" size={16} />正在检查腾讯云和阿里云全部地区；已完成地区的机器会立即出现在列表中。</div>}
      {(syncCloud.data?.errors?.length ?? 0) > 0 && <div className="inline-alert" role="status"><AlertTriangle size={16} />已同步其余地区，另有 {syncCloud.data!.errors.length} 个地区暂时查询失败，可稍后再次同步。</div>}
      <div className="toolbar">
        <label className="search-field"><Search size={16} /><span className="sr-only">搜索目标</span><input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索名称、框架或硬件" /></label>
        <label className="select-field"><Filter size={15} /><select aria-label="资源状态" value={status} onChange={e => setStatus(e.target.value)}>
          <option value="active">活跃资源</option><option value="missing">云端不可见</option><option value="archived">历史归档</option><option value="online">Worker 在线</option><option value="inventory">仅库存</option><option value="degraded">异常</option><option value="offline">离线</option><option value="all">全部状态</option>
        </select></label>
        <span className="result-count">{items.length} 个资源</span>
      </div>
      {query.isLoading ? <LoadingState /> : query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : items.length ? (
        <section className="panel table-panel"><div className="table-wrap"><table>
          <thead><tr><th>资源</th><th>执行状态</th><th>资源生命周期</th><th>运行时</th><th>硬件</th><th>最后确认</th><th>端点</th><th>SSH 连接</th><th><span className="sr-only">操作</span></th></tr></thead>
          <tbody>{items.map(x => (
            <tr key={x.id}>
              <td><div className="resource-name"><span className="resource-icon"><Server size={16} /></span><div><strong>{x.name}</strong><span className="cell-meta">{x.type || x.id}</span></div></div></td>
              <td><TargetStatus status={x.status} /></td>
              <td>{lifecycleLabel(x)}{x.missingSince && <span className="cell-meta">缺失始于 {formatDate(x.missingSince)}</span>}</td>
              <td>{x.framework || '—'}<span className="cell-meta">{x.version || ''}</span></td>
              <td><span className="inline-icon"><Cpu size={14} />{x.hardware || '—'}</span></td>
              <td>{formatDate(x.lastInventorySeenAt || x.lastSeenAt)}</td>
              <td>{x.endpoint?.startsWith('http') ? <a className="text-link" href={x.endpoint} target="_blank" rel="noreferrer">打开<ExternalLink size={14} /></a> : x.endpoint ? <code>{x.endpoint}</code> : '—'}</td>
              <td><TargetSshButton target={x} onConfigure={() => setSshTarget(x)} /></td>
              <td><div className="table-actions">{(CLOUD_PROVIDERS.has(x.type || '') || x.type === 'external') && x.lifecycleStatus === 'active'
                ? <button className="button danger-ghost compact-button" onClick={() => setDestroyTarget(x)} aria-label={`销毁 ${x.name}`}><Trash2 size={14} />销毁</button>
                : null}</div></td>
            </tr>
          ))}</tbody>
        </table></div></section>
      ) : <EmptyState title="没有匹配的候选资源" description={status === 'active' ? '同步云库存后，云端不可见的资源会移入历史筛选。' : undefined} />}
      <ImportTargetDialog open={importOpen} onClose={() => setImportOpen(false)} />
      <ImportTargetDialog target={sshTarget} open={Boolean(sshTarget)} onClose={() => setSshTarget(null)} />
      <TargetDestroyDialog target={destroyTarget} onClose={() => setDestroyTarget(null)} />
    </div>
  );
}

function TargetDestroyDialog({ target, onClose }: { target: Target | null; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [phrase, setPhrase] = useState('');
  const preview = useQuery({
    queryKey: ['target-destroy-preview', target?.id],
    queryFn: () => api.targetDestroyPreview(target!.id),
    enabled: Boolean(target),
  });
  const destroy = useMutation({
    mutationFn: () => api.destroyTarget(target!.id, phrase),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['targets'] });
      onClose();
    },
  });
  useEffect(() => { if (!target) { setPhrase(''); destroy.reset(); } }, [target]);
  if (!target) return null;
  const data: TargetDestroyPreview | undefined = preview.data;
  const ready = Boolean(data && phrase === data.acknowledgement);
  return (
    <div className="operator-overlay" role="presentation" onMouseDown={() => { if (!destroy.isPending) onClose(); }}>
      <div className="operator-dialog" role="dialog" aria-modal="true" aria-labelledby="destroy-target-title" onMouseDown={e => e.stopPropagation()}>
        <div className="operator-dialog-heading">
          <div><span className="eyebrow">DESTROY RESOURCE</span><h2 id="destroy-target-title">一键销毁云实例</h2></div>
          <button className="icon-button" type="button" onClick={onClose} aria-label="关闭" disabled={destroy.isPending}><X size={18} /></button>
        </div>
        {preview.isLoading ? <LoadingState /> : preview.isError ? <ErrorState error={preview.error} onRetry={() => preview.refetch()} /> : data ? (<>
          <div className="notice danger"><TriangleAlert size={18} /><div><strong>不可逆操作</strong><p>将销毁实例并释放其系统盘、本地盘（含机械盘）、公网，以及 Looper 纳管的子网/安全组等随附资源。请确认没有正在运行的关键工作负载。</p></div></div>
          <div className="destroy-target-summary">
            <span>实例名称</span><strong>{data.instanceName}</strong>
            <span>实例 ID</span><code>{data.instanceId}</code>
            <span>地域</span><code>{data.region}</code>
          </div>
          <div className="destroy-resource-list">
            {data.resources.map(resource => (
              <div key={`${resource.kind}:${resource.id}`} className="destroy-resource-row">
                <span className={`destroy-resource-kind ${resource.kind}`}>{DESTROY_KIND_LABELS[resource.kind] || resource.kind}</span>
                <code>{resource.id}</code>
                <small>{resource.note}</small>
              </div>
            ))}
          </div>
          <div className="destroy-acknowledgement"><span>确认文本</span><code>{data.acknowledgement}</code></div>
          <label className="destroy-confirm-field"><span>原样输入确认文本</span><input value={phrase} onChange={e => setPhrase(e.target.value)} placeholder="输入上方完整确认文本" autoComplete="off" /></label>
          {destroy.isError && <div className="error-banner">{destroy.error instanceof Error ? destroy.error.message : '销毁失败'}</div>}
          <div className="action-row">
            <button className="button" type="button" onClick={onClose} disabled={destroy.isPending}>取消</button>
            <button className="button danger-button" type="button" disabled={destroy.isPending || !ready} onClick={() => destroy.mutate()}>
              {destroy.isPending ? <LoaderCircle className="spin" size={15} /> : <Trash2 size={15} />}{destroy.isPending ? '销毁中…' : '确认销毁'}
            </button>
          </div>
        </>) : null}
      </div>
    </div>
  );
}
