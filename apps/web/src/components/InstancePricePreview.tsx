import { BadgeDollarSign } from 'lucide-react';
import { estimateInstancePrice, formatPreviewHourly, formatPreviewMonthly } from '../lib/cloudPricing';
import type { CloudInstanceType } from '../lib/types';

export function InstancePricePreview({ item, compact = false }: { item: CloudInstanceType; compact?: boolean }) {
  const price = estimateInstancePrice(item);
  const hourly = formatPreviewHourly(price.hourlyAmount);
  const monthly = formatPreviewMonthly(price.monthlyAmount);
  return <div className={`instance-price-preview ${compact ? 'compact' : ''}`} aria-label={`预估价格约 ${hourly} 元每小时，月约 ${monthly} 元`}>
    {!compact && <BadgeDollarSign size={17} />}
    <span><small>预估价</small><strong>约 ¥{hourly}<i>/小时</i></strong></span>
    <em>月约 ¥{monthly} · 仅计算规格</em>
  </div>;
}
