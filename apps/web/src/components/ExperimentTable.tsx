import { ArrowUpRight } from 'lucide-react';
import { Link } from 'react-router-dom';
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
  return <div className="table-wrap"><table>
    <thead><tr><th>研究</th><th>状态</th><th>场景 / 候选资源</th><th>结论</th><th>进度</th><th>更新时间</th><th><span className="sr-only">操作</span></th></tr></thead>
    <tbody>{experiments.map(item => <tr key={item.id}>
      <td><Link className="primary-link" to={`/experiments/${item.id}`}>{item.name || `研究 ${item.id}`}</Link><span className="cell-meta">{item.owner || item.id}</span></td>
      <td><StatusBadge status={item.status} /></td>
      <td>{item.benchmarkName || item.benchmarkId || '未指定场景'}<span className="cell-meta">{item.targetNames?.join(' · ') || item.targetName || item.targetId || '未指定候选资源'}</span></td>
      <td className="metric-cell">{resultLabel(item)}</td>
      <td><div className="progress-label"><span>{Math.round(item.progress ?? 0)}%</span><span>{item.attempts ?? 0}/{item.maxAttempts ?? '—'}</span></div><div className="progress"><span style={{ width: `${Math.min(100, Math.max(0, item.progress ?? 0))}%` }} /></div></td>
      <td>{formatDate(item.updatedAt || item.createdAt)}</td>
      <td><Link className="icon-button" to={`/experiments/${item.id}`} aria-label={`查看 ${item.name}`} title="查看详情"><ArrowUpRight size={17} /></Link></td>
    </tr>)}</tbody>
  </table></div>;
}
