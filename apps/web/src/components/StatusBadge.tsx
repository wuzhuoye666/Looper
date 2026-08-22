import type { ExperimentStatus } from '../lib/types';
import { statusLabel } from '../lib/format';

export function StatusBadge({ status }: { status?: ExperimentStatus | string }) {
  const normalized = status || 'draft';
  return <span className={`status status-${normalized}`}><span aria-hidden="true" />{statusLabel[normalized as ExperimentStatus] || normalized}</span>;
}
export function TargetStatus({ status = 'unknown' }: { status?: string }) {
  const labels: Record<string, string> = { online: '可运行', inventory: '仅库存', offline: '离线', degraded: '异常', unknown: '未知' };
  return <span className={`status target-${status}`}><span aria-hidden="true" />{labels[status] || status}</span>;
}
