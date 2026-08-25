import type { CloudInstanceType, CloudProviderId } from './types';

const HOURS_PER_MONTH = 730;
const PRICE_CALIBRATION_FACTOR = 2;
export const DEFAULT_PREVIEW_SYSTEM_DISK_GIB = 50;
export const DEFAULT_PREVIEW_PUBLIC_BANDWIDTH_MBPS = 1;

const resourceRates: Record<CloudProviderId, {
  cpu: number;
  memory: number;
  gpu: number;
  localStorageGib: number;
  systemDiskGib: number;
  publicIp: number;
  publicBandwidthMbps: number;
}> = {
  tencent: { cpu: 0.046, memory: 0.014, gpu: 1.65, localStorageGib: 0.00014, systemDiskGib: 0.00055, publicIp: 0.012, publicBandwidthMbps: 0.018 },
  alibaba: { cpu: 0.049, memory: 0.015, gpu: 1.75, localStorageGib: 0.00015, systemDiskGib: 0.00058, publicIp: 0.013, publicBandwidthMbps: 0.020 },
  volcengine: { cpu: 0.044, memory: 0.013, gpu: 1.6, localStorageGib: 0.00013, systemDiskGib: 0.00052, publicIp: 0.011, publicBandwidthMbps: 0.017 },
  baidu: { cpu: 0.047, memory: 0.014, gpu: 1.68, localStorageGib: 0.00014, systemDiskGib: 0.00056, publicIp: 0.012, publicBandwidthMbps: 0.019 },
};

export interface InstancePricePreviewValue {
  hourlyAmount: number;
  monthlyAmount: number;
  currency: 'CNY';
  systemDiskGib: number;
  publicBandwidthMbps: number;
  breakdown: {
    instance: number;
    systemDisk: number;
    publicIp: number;
  };
}

function instanceGeneration(item: CloudInstanceType) {
  const identity = [item.familyToken, item.family, item.id].filter(Boolean).join('.');
  const matches = [...identity.matchAll(/(?:^|[._-])[a-z]+(\d{1,2})[a-z]*(?=$|[._-])/gi)];
  const generations = matches.map(match => Number(match[1])).filter(value => value >= 1 && value <= 25);
  return generations.length ? Math.max(...generations) : undefined;
}

function instanceClassMultiplier(item: CloudInstanceType) {
  const kind = `${item.typeKind || ''} ${item.typeLabel || ''} ${item.familyLabel || ''}`.toLowerCase();
  if (/bare|metal|裸金属|hpc|高性能计算/.test(kind)) return 1.2;
  if (/gpu|异构|加速/.test(kind)) return 1.14;
  if (/local|storage|本地|存储/.test(kind)) return 1.12;
  if (/memory|内存/.test(kind)) return 1.1;
  if (/compute|计算/.test(kind)) return 1.08;
  if (/burst|突发/.test(kind)) return 0.9;
  return 1;
}

function roundHourly(value: number) {
  return Math.round(value * 1000) / 1000;
}

export function estimateInstancePrice(
  item: CloudInstanceType,
  options: { systemDiskGib?: number; publicIp?: boolean; publicBandwidthMbps?: number } = {},
): InstancePricePreviewValue {
  const rates = resourceRates[item.provider];
  const localStorageGib = Math.max(item.localStorageCapacityGib || 0, 0);
  const systemDiskGib = Math.max(options.systemDiskGib ?? DEFAULT_PREVIEW_SYSTEM_DISK_GIB, 0);
  const publicIp = options.publicIp ?? true;
  const publicBandwidthMbps = publicIp
    ? Math.max(options.publicBandwidthMbps ?? DEFAULT_PREVIEW_PUBLIC_BANDWIDTH_MBPS, 0)
    : 0;
  const generation = instanceGeneration(item);
  const generationMultiplier = generation == null ? 1 : Math.min(1.2, Math.max(0.95, 0.9 + generation * 0.025));
  const instance = Math.max(
    item.cpu * rates.cpu
      + item.memoryGib * rates.memory
      + Math.max(item.gpu || 0, 0) * rates.gpu
      + localStorageGib * rates.localStorageGib,
    0.01,
  ) * generationMultiplier * instanceClassMultiplier(item) * PRICE_CALIBRATION_FACTOR;
  const systemDisk = systemDiskGib * rates.systemDiskGib * PRICE_CALIBRATION_FACTOR;
  const publicIpAmount = publicIp
    ? (rates.publicIp + publicBandwidthMbps * rates.publicBandwidthMbps) * PRICE_CALIBRATION_FACTOR
    : 0;
  const hourlyAmount = roundHourly(instance + systemDisk + publicIpAmount);
  return {
    hourlyAmount,
    monthlyAmount: Math.round(hourlyAmount * HOURS_PER_MONTH * 100) / 100,
    currency: 'CNY',
    systemDiskGib,
    publicBandwidthMbps,
    breakdown: {
      instance: roundHourly(instance),
      systemDisk: roundHourly(systemDisk),
      publicIp: roundHourly(publicIpAmount),
    },
  };
}

export function formatPreviewHourly(value: number) {
  return value < 1 ? value.toFixed(3) : value.toFixed(2);
}

export function formatPreviewMonthly(value: number) {
  return new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 0 }).format(value);
}
