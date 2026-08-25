import { BadgeDollarSign } from 'lucide-react';
import { estimateInstancePrice, formatPreviewHourly, formatPreviewMonthly } from '../lib/cloudPricing';
import type { CloudInstanceType, SelectionRecommendationPrice } from '../lib/types';

const HOURS_PER_MONTH = 730;

function sourceLabel(source?: SelectionRecommendationPrice['source']) {
  if (source === 'price-table') return '价格表';
  if (source === 'live') return '实时价';
  return '价格不可用';
}

export function InstancePricePreview({
  item,
  compact = false,
  price: priceOverride,
}: {
  item: CloudInstanceType;
  compact?: boolean;
  price?: SelectionRecommendationPrice;
}) {
  if (!priceOverride) {
    const estimated = estimateInstancePrice(item);
    const hourly = formatPreviewHourly(estimated.hourlyAmount);
    const monthly = formatPreviewMonthly(estimated.monthlyAmount);
    const included = estimated.systemDiskGib + ' GiB 系统盘、' + estimated.publicBandwidthMbps + ' Mbps 公网 IP';
    return <div className={['instance-price-preview', compact ? 'compact' : ''].filter(Boolean).join(' ')} aria-label={'预估价格约 ' + hourly + ' 元每小时，月约 ' + monthly + ' 元，已包含 ' + included}>
      {!compact && <BadgeDollarSign size={17} />}
      <span><small>预估价</small><strong>约 ¥{hourly}<i>/小时</i></strong></span>
      <em>月约 ¥{monthly} · 含 {included}</em>
    </div>;
  }
  if (priceOverride.source !== 'price-table' && priceOverride.source !== 'live') {
    return <div className={['instance-price-preview', compact ? 'compact' : '', 'unavailable'].filter(Boolean).join(' ')} aria-label='价格不可用，请重试实时询价'>
      {!compact && <BadgeDollarSign size={17} />}
      <span><small>价格不可用</small><strong>—</strong></span>
      <em>点击重试实时询价</em>
    </div>;
  }
  const hourlyAmount = Number(priceOverride.hourlyAmount);
  const monthlyAmount = priceOverride.monthlyAmount
    ? Number(priceOverride.monthlyAmount)
    : hourlyAmount * HOURS_PER_MONTH;
  const hourly = formatPreviewHourly(hourlyAmount);
  const monthly = formatPreviewMonthly(monthlyAmount);
  const label = sourceLabel(priceOverride.source);
  const aria = label + '约 ' + hourly + ' 元每小时，月约 ' + monthly + ' 元';
  return <div className={['instance-price-preview', compact ? 'compact' : '', priceOverride.source === 'live' ? 'live' : ''].filter(Boolean).join(' ')} aria-label={aria}>
    {!compact && <BadgeDollarSign size={17} />}
    <span><small>{label}</small><strong>约 ¥{hourly}<i>/小时</i></strong></span>
    <em>月约 ¥{monthly}</em>
  </div>;
}