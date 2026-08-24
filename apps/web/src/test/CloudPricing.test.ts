import { describe, expect, it } from 'vitest';
import { estimateInstancePrice, formatPreviewHourly, formatPreviewMonthly } from '../lib/cloudPricing';

describe('云机型预览价格', () => {
  it('按云厂商 CPU 与内存资源生成稳定的小时和月度估算', () => {
    const price = estimateInstancePrice({
      provider: 'tencent', region: 'ap-test', id: 'S9.TEST', cpu: 4, memoryGib: 8, zones: [],
    });
    expect(price).toEqual({ hourlyAmount: 0.248, monthlyAmount: 181.04, currency: 'CNY' });
    expect(formatPreviewHourly(price.hourlyAmount)).toBe('0.248');
    expect(formatPreviewMonthly(price.monthlyAmount)).toBe('181');
  });

  it('将 GPU 和本地盘计入规格预估，但不引入网络或镜像价格', () => {
    const price = estimateInstancePrice({
      provider: 'alibaba', region: 'cn-test', id: 'ecs.gn.test', cpu: 8, memoryGib: 32,
      gpu: 1, localStorageCapacityGib: 1900, zones: [],
    });
    expect(price.hourlyAmount).toBe(2.541);
    expect(formatPreviewHourly(price.hourlyAmount)).toBe('2.54');
  });
});
