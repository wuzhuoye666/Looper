import type { ExperimentStatus } from './types';

export const statusLabel: Record<ExperimentStatus, string> = {
  draft: '草稿', queued: '排队中', running: '运行中', paused: '已暂停', completed: '已完成', failed: '失败', cancelled: '已取消',
};
export const formatDate = (value?: string) => value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '—';
export const formatNumber = (value?: number, digits = 2) => value == null || Number.isNaN(value) ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(value);
export const scoreDelta = (score?: number, baseline?: number) => score == null || baseline == null || baseline === 0 ? undefined : ((score - baseline) / Math.abs(baseline)) * 100;
