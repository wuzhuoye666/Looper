import type { ExperimentStatus } from './types';

export const statusLabel: Record<ExperimentStatus, string> = {
  draft: '草稿', queued: '排队中', running: '运行中', paused: '已暂停', completed: '已完成', failed: '失败', cancelled: '已取消',
};

export const PLATFORM_TIME_ZONE = 'Asia/Shanghai';

const parseApiDate = (value?: string) => {
  if (!value?.trim()) return undefined;
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? undefined : date;
};

const formatParts = (value?: string) => {
  const date = parseApiDate(value);
  if (!date) return undefined;
  return Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: PLATFORM_TIME_ZONE,
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      hourCycle: 'h23',
    }).formatToParts(date).filter(part => part.type !== 'literal').map(part => [part.type, part.value]),
  ) as Record<string, string>;
};

export const formatDate = (value?: string) => {
  const parts = formatParts(value);
  return parts ? `${parts.month}/${parts.day} ${parts.hour}:${parts.minute}` : '—';
};
export const formatDateTime = (value?: string) => {
  const parts = formatParts(value);
  return parts ? `${parts.year}/${parts.month}/${parts.day} ${parts.hour}:${parts.minute}:${parts.second}` : '—';
};
export const formatTime = (value?: string) => {
  const parts = formatParts(value);
  return parts ? `${parts.hour}:${parts.minute}:${parts.second}` : '—';
};
export const formatNumber = (value?: number | null, digits = 2) => value == null || Number.isNaN(value) ? '—' : new Intl.NumberFormat('zh-CN', { maximumFractionDigits: digits }).format(value);
export const scoreDelta = (score?: number, baseline?: number) => score == null || baseline == null || baseline === 0 ? undefined : ((score - baseline) / Math.abs(baseline)) * 100;
