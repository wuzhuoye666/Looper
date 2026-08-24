import { describe, expect, it } from 'vitest';
import { formatDate, formatDateTime, formatTime, PLATFORM_TIME_ZONE } from '../lib/format';

describe('平台时间格式化', () => {
  const utcTime = '2026-08-20T14:05:06Z';

  it('固定使用北京时间', () => {
    expect(PLATFORM_TIME_ZONE).toBe('Asia/Shanghai');
    expect(formatDate(utcTime)).toBe('08/20 22:05');
    expect(formatDateTime(utcTime)).toBe('2026/08/20 22:05:06');
    expect(formatTime(utcTime)).toBe('22:05:06');
  });

  it('将 API 无时区时间按 UTC 转换为北京时间', () => {
    expect(formatDate('2026-08-24T01:53:23.940536')).toBe('08/24 09:53');
    expect(formatDateTime('2026-08-24T01:53:23.940536')).toBe('2026/08/24 09:53:23');
    expect(formatTime('2026-08-24T01:53:23.940536')).toBe('09:53:23');
  });

  it('保留时间字符串中的显式偏移量', () => {
    expect(formatDateTime('2026-08-24T09:53:23+08:00')).toBe('2026/08/24 09:53:23');
  });

  it('为空值保留缺省占位符', () => {
    expect(formatDate()).toBe('—');
    expect(formatDateTime()).toBe('—');
    expect(formatTime()).toBe('—');
  });
});
