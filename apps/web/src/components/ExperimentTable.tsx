import { ArrowUpRight, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { api } from '../lib/api';
import { formatDate, formatNumber } from '../lib/format';
import type { Experiment } from '../lib/types';
import { StatusBadge } from './StatusBadge';

const PROTECTED_STATUSES = ['queued', 'running', 'paused'];

function resultLabel(item: Experiment) {
  if (item.mode !== 'selection') return formatNumber(item.bestScore);
  if (item.resultConclusion) return item.resultConclusion;
  if (item.comparison?.winner) return item.comparison.winner;
  if (item.comparison?.status === 'insufficient_evidence') return '证据不足';
  if (item.status === 'running') return '测试中';
  if (item.status === 'queued') return '排队中';
  if (item.status === 'failed') return '执行失败';
  if (item.status === 'cancelled') return '已取消';
  return item.comparison?.conclusion_strength || '待执行';
}

export function ExperimentTable({ experiments, selectable = false }: { experiments: Experiment[]; selectable?: boolean }) {
  const queryClient = useQueryClient();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const deletion = useMutation({
    mutationFn: (id: string) => api.deleteExperiment(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['experiments'] }),
  });
  const bulkDeletion = useMutation({
    mutationFn: (ids: string[]) => Promise.all(ids.map(id => api.deleteExperiment(id))),
    onSuccess: () => {
      setSelected(new Set());
      queryClient.invalidateQueries({ queryKey: ['experiments'] });
    },
  });

  const deletable = useMemo(() => experiments.filter(item => !PROTECTED_STATUSES.includes(item.status)), [experiments]);
  const selectedIds = useMemo(() => deletable.filter(item => selected.has(item.id)), [deletable, selected]);
  const allSelected = deletable.length > 0 && deletable.every(item => selected.has(item.id));
  const someSelected = selectedIds.length > 0;

  const remove = (item: Experiment) => {
    if (PROTECTED_STATUSES.includes(item.status)) return;
    if (window.confirm(`确定删除“${item.name}”吗？研究记录将从列表移除，此操作不可撤销。`)) deletion.mutate(item.id);
  };

  const toggle = (id: string) => {
    setSelected(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleAll = () => {
    setSelected(prev => {
      const next = new Set(prev);
      if (allSelected) deletable.forEach(item => next.delete(item.id));
      else deletable.forEach(item => next.add(item.id));
      return next;
    });
  };

  const clearSelection = () => setSelected(new Set());

  const bulkRemove = () => {
    const ids = selectedIds.map(item => item.id);
    if (!ids.length) return;
    const preview = selectedIds.slice(0, 3).map(item => item.name).join('、');
    const suffix = selectedIds.length > 3 ? ` 等 ${selectedIds.length} 项` : '';
    if (window.confirm(`确定删除选中的 ${selectedIds.length} 项研究吗？${preview}${suffix}。此操作不可撤销。`)) {
      bulkDeletion.mutate(ids);
    }
  };

  return <div className="table-wrap">
    {selectable && someSelected && <div className="table-selection-bar" role="status" aria-live="polite">
      <span>已选 {selectedIds.length} 项</span>
      <div className="table-selection-actions">
        <button className="button secondary" type="button" onClick={clearSelection} disabled={bulkDeletion.isPending}>取消选择</button>
        <button className="button danger-button" type="button" onClick={bulkRemove} disabled={bulkDeletion.isPending}><Trash2 size={15}/>删除所选</button>
      </div>
    </div>}
    <table>
      <thead><tr>
        {selectable && <th className="select-cell"><input type="checkbox" ref={el => { if (el) { el.checked = allSelected; el.indeterminate = someSelected && !allSelected; } }} onChange={toggleAll} disabled={deletable.length === 0} aria-label={allSelected ? '取消全选' : '全选'} /></th>}
        <th>研究</th><th>状态</th><th>场景 / 候选资源</th><th>结论</th><th>进度</th><th>更新时间</th><th><span className="sr-only">操作</span></th>
      </tr></thead>
      <tbody>{experiments.map(item => {
        const protectedRow = PROTECTED_STATUSES.includes(item.status);
        const checked = selected.has(item.id);
        return <tr key={item.id} className={checked ? 'selected-row' : undefined}>
          {selectable && <td className="select-cell"><input type="checkbox" checked={checked} onChange={() => toggle(item.id)} disabled={protectedRow} aria-label={protectedRow ? `${item.name}（运行中不可删除）` : `选择 ${item.name}`} /></td>}
          <td><Link className="primary-link" to={`/experiments/${item.id}`}>{item.name || `研究 ${item.id}`}</Link><span className="cell-meta">{item.owner || item.id}</span></td>
          <td><StatusBadge status={item.status} /></td>
          <td>{item.benchmarkName || item.benchmarkId || '未指定场景'}<span className="cell-meta">{item.targetNames?.join(' · ') || item.targetName || item.targetId || '未指定候选资源'}</span></td>
          <td className="metric-cell">{resultLabel(item)}</td>
          <td><div className="progress-label"><span>{Math.round(item.progress ?? 0)}%</span><span>{item.attempts ?? 0}/{item.maxAttempts ?? '—'}</span></div><div className="progress"><span style={{ width: `${Math.min(100, Math.max(0, item.progress ?? 0))}%` }} /></div></td>
          <td>{formatDate(item.updatedAt || item.createdAt)}</td>
          <td><div className="table-actions"><Link className="icon-button" to={`/experiments/${item.id}`} aria-label={`查看 ${item.name}`} title="查看详情"><ArrowUpRight size={17} /></Link><button className="icon-button danger-ghost" type="button" disabled={deletion.isPending || protectedRow} onClick={() => remove(item)} aria-label={`删除 ${item.name}`} title={protectedRow ? '请先取消运行中的研究' : '删除研究'}><Trash2 size={16}/></button></div></td>
        </tr>;
      })}</tbody>
    </table>
  </div>;
}
