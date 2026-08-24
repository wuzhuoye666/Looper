import type { CloudInstanceType, CloudProviderId } from './types';

const HOURS_PER_MONTH = 730;

const resourceRates: Record<CloudProviderId, {
  cpu: number;
  memory: number;
  gpu: number;
  localStorageGib: number;
}> = {
  tencent: { cpu: 0.038, memory: 0.012, gpu: 1.45, localStorageGib: 0.00012 },
  alibaba: { cpu: 0.041, memory: 0.013, gpu: 1.55, localStorageGib: 0.00013 },
  volcengine: { cpu: 0.036, memory: 0.011, gpu: 1.4, localStorageGib: 0.00011 },
  baidu: { cpu: 0.039, memory: 0.012, gpu: 1.48, localStorageGib: 0.00012 },
};

export interface InstancePricePreviewValue {
  hourlyAmount: number;
  monthlyAmount: number;
  currency: 'CNY';
}

export function estimateInstancePrice(item: CloudInstanceType): InstancePricePreviewValue {
  const rates = resourceRates[item.provider];
  const localStorageGib = Math.max(item.localStorageCapacityGib || 0, 0);
  const rawHourly = Math.max(
    item.cpu * rates.cpu
      + item.memoryGib * rates.memory
      + Math.max(item.gpu || 0, 0) * rates.gpu
      + localStorageGib * rates.localStorageGib,
    0.01,
  );
  const hourlyAmount = Math.round(rawHourly * 1000) / 1000;
  return {
    hourlyAmount,
    monthlyAmount: Math.round(hourlyAmount * HOURS_PER_MONTH * 100) / 100,
    currency: 'CNY',
  };
}

export function formatPreviewHourly(value: number) {
  return value < 1 ? value.toFixed(3) : value.toFixed(2);
}

export function formatPreviewMonthly(value: number) {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value);
}
