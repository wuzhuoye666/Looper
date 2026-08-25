import { describe, expect, it } from 'vitest';
import { estimateInstancePrice, formatPreviewHourly, formatPreviewMonthly } from '../lib/cloudPricing';

describe('云机型预览价格', () => {
  it('按云厂商和机型代际生成包含默认磁盘与公网 IP 的总价估算', () => {
    const price = estimateInstancePrice({
      provider: 'tencent', region: 'ap-test', id: 'S9.TEST', cpu: 4, memoryGib: 8, zones: [],
    });
    expect(price).toEqual({
      hourlyAmount: 0.781, monthlyAmount: 570.13, currency: 'CNY', systemDiskGib: 50,
      publicBandwidthMbps: 1, breakdown: { instance: 0.666, systemDisk: 0.055, publicIp: 0.06 },
    });
    expect(formatPreviewHourly(price.hourlyAmount)).toBe('0.781');
    expect(formatPreviewMonthly(price.monthlyAmount)).toBe('570');
  });

  it('将 GPU、本地盘、系统盘和公网 IP 都计入预估', () => {
    const price = estimateInstancePrice({
      provider: 'alibaba', region: 'cn-test', id: 'ecs.gn.test', cpu: 8, memoryGib: 32,
      gpu: 1, localStorageCapacityGib: 1900, zones: [],
    });
    expect(price.hourlyAmount).toBeGreaterThan(5.9);
    expect(price.breakdown.systemDisk).toBe(0.058);
    expect(price.breakdown.publicIp).toBe(0.066);
  });

  it('同配置的不同代际机型不再全部同价', () => {
    const base = { provider: 'tencent' as const, region: 'ap-test', cpu: 2, memoryGib: 2, zones: [] };
    const s4 = estimateInstancePrice({ ...base, id: 'S4.MEDIUM2', family: 'S4' });
    const s9 = estimateInstancePrice({ ...base, id: 'S9.MEDIUM2', family: 'S9' });
    expect(s9.hourlyAmount).toBeGreaterThan(s4.hourlyAmount);
    expect(s4.hourlyAmount).toBeGreaterThan(0.3);
  });
});
