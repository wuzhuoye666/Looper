import { useQuery } from '@tanstack/react-query';
import { ArrowRight, Gauge, Plus } from 'lucide-react';
import { Link } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';
import type { CapacityStudyStatus } from '../lib/types';

const statusLabel: Record<CapacityStudyStatus, string> = {
  draft: '草稿', queued: '排队中', deploying: '部署中', running: '运行中', resetting: '重置中',
  cleaning: '清理中', cancelling: '取消并清理', completed: '已完成', failed: '失败',
  cancelled: '已取消', 'needs-attention': '需要人工处理',
};

export function CapacityListPage() {
  const query = useQuery({ queryKey: ['capacity-studies'], queryFn: api.capacityStudies, refetchInterval: 5000 });
  const items = query.data?.items || [];
  return <div className="page capacity-list-page">
    <PageHeader title="容量测试" description="从接口合同构建真实业务链路，分别测量内网与公网 SLO 容量边界。" actions={<Link className="button secondary" to="/interfaces"><Plus size={15}/>从接口发现创建</Link>}/>
    {query.isLoading ? <LoadingState label="正在读取容量测试"/> : query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()}/> : !items.length ? <EmptyState title="还没有容量测试" description="先完成一次动态接口发现，再从发现记录创建容量测试。" action={<Link className="button primary" to="/interfaces"><Gauge size={16}/>前往接口发现</Link>}/> : <section className="panel table-panel">
      <div className="table-wrap"><table><thead><tr><th>名称</th><th>源码</th><th>状态</th><th>进度</th><th>更新时间</th><th/></tr></thead><tbody>{items.map(item => <tr key={item.id}>
        <td><strong>{item.name}</strong><span className="cell-meta">{item.id}</span></td>
        <td>{item.discoveryName || item.discoveryId}<span className="cell-meta">{item.sourceArchive.status === 'retained' ? '源码已加密保留' : '源码已过期'}</span></td>
        <td><span className={`status status-${item.status}`}><span/>{statusLabel[item.status]}</span></td>
        <td>{item.status === 'draft' ? `第 ${item.currentStep + 1} / 5 步` : item.execution.currentNetwork === 'external' ? '公网测试' : item.status === 'completed' ? '报告已生成' : '内网测试'}</td>
        <td>{new Date(item.updatedAt).toLocaleString()}</td>
        <td><Link className="icon-button" aria-label={`打开 ${item.name}`} to={`/capacity/${encodeURIComponent(item.id)}`}><ArrowRight size={16}/></Link></td>
      </tr>)}</tbody></table></div>
    </section>}
  </div>;
}
