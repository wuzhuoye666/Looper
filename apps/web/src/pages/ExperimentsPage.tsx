import { useQuery } from '@tanstack/react-query';
import { Filter, Plus, Search, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ExperimentTable } from '../components/ExperimentTable';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';

const statuses = [{value:'all',label:'全部状态'},{value:'running',label:'运行中'},{value:'paused',label:'已暂停'},{value:'completed',label:'已完成'},{value:'failed',label:'失败'},{value:'draft',label:'草稿'}];
export function ExperimentsPage() {
  const [search, setSearch] = useState(''); const [status, setStatus] = useState('all');
  const query = useQuery({ queryKey: ['experiments'], queryFn: () => api.experiments(), refetchInterval: 15_000 });
  const items = useMemo(() => (query.data?.items || []).filter(x => (status === 'all' || x.status === status) && (!search || `${x.name} ${x.id} ${x.targetName || ''}`.toLowerCase().includes(search.toLowerCase()))), [query.data, search, status]);
  return <div className="page"><PageHeader title="选型研究" description="管理采购问题、候选资源和场景证据。" actions={<Link className="button primary" to="/experiments/new"><Plus size={16}/>新建选型研究</Link>} />
    <div className="toolbar"><label className="search-field"><Search size={16}/><span className="sr-only">搜索选型研究</span><input value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索名称、ID 或候选资源" />{search && <button onClick={() => setSearch('')} aria-label="清除搜索"><X size={15}/></button>}</label><label className="select-field"><Filter size={15}/><span className="sr-only">筛选状态</span><select value={status} onChange={e => setStatus(e.target.value)}>{statuses.map(s => <option key={s.value} value={s.value}>{s.label}</option>)}</select></label><span className="result-count">{items.length} 个结果</span></div>
    {query.isLoading ? <LoadingState /> : query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : items.length ? <section className="panel table-panel"><ExperimentTable experiments={items} selectable /></section> : <EmptyState title="没有匹配的研究" description={search || status !== 'all' ? '请调整搜索词或状态筛选。' : '先创建一项场景化选型研究。'} action={!search && status === 'all' ? <Link className="button primary" to="/experiments/new">新建选型研究</Link> : undefined} />}
  </div>;
}
