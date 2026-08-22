import { ArrowUpRight, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { api } from '../lib/api';
import { formatDate, formatNumber } from '../lib/format';
import type { Experiment } from '../lib/types';
import { StatusBadge } from './StatusBadge';

function resultLabel(item: Experiment) {
  if (item.mode !== 'selection') return formatNumber(item.bestScore);
  if (item.comparison?.winner) return item.comparison.winner;
  if (item.comparison?.status === 'insufficient_evidence') return '证据不足';
  return item.comparison?.conclusion_strength || '待执行';
}

export function ExperimentTable({ experiments }: { experiments: Experiment[] }) {
  const queryClient = useQueryClient();
  const deletion = useMutation({
    mutationFn: (id: string) => api.deleteExperiment(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['experiments'] }),
  });
  const remove = (item: Experiment) => {
    if (item.status === 'queued' || item.status === 'running' || item.status === 'paused') return;
    if (window.confirm(`确定删除“${item.name}”吗？研究记录将从列表移除，此操作不可撤销。`)) deletion.mutate(item.id);
  };
  return <div className="table-wrap"><table>
    <thead><tr><th>研究</th><th>状态</th><th>场景 / 候选资源</th><th>结论</th><th>进度</th><th>更新时间</th><th><span className="sr-only">操作</span></th></tr></thead>
    <tbody>{experiments.map(item => <tr key={item.id}>
      <td><Link className="primary-link" to={`/experiments/${item.id}`}>{item.name || `研究 ${item.id}`}</Link><span className="cell-meta">{item.owner || item.id}</span></td>
      <td><StatusBadge status={item.status} /></td>
      <td>{item.benchmarkName || item.benchmarkId || '未指定场景'}<span className="cell-meta">{item.targetNames?.join(' · ') || item.targetName || item.targetId || '未指定候选资源'}</span></td>
      <td className="metric-cell">{resultLabel(item)}</td>
      <td><div className="progress-label"><span>{Math.round(item.progress ?? 0)}%</span><span>{item.attempts ?? 0}/{item.maxAttempts ?? '—'}</span></div><div className="progress"><span style={{ width: `${Math.min(100, Math.max(0, item.progress ?? 0))}%` }} /></div></td>
      <td>{formatDate(item.updatedAt || item.createdAt)}</td>
      <td><div className="table-actions"><Link className="icon-button" to={`/experiments/${item.id}`} aria-label={`查看 ${item.name}`} title="查看详情"><ArrowUpRight size={17} /></Link><button className="icon-button danger-ghost" type="button" disabled={deletion.isPending || ['queued','running','paused'].includes(item.status)} onClick={() => remove(item)} aria-label={`删除 ${item.name}`} title={['queued','running','paused'].includes(item.status) ? '请先取消运行中的研究' : '删除研究'}><Trash2 size={16}/></button></div></td>
    </tr>)}</tbody>
  </table></div>;
}
