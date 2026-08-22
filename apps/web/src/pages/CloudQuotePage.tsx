import { useMutation, useQuery } from '@tanstack/react-query';
import { ClipboardCheck, RefreshCw, ShoppingCart } from 'lucide-react';
import { useRef } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';

function idempotencyKey() {
  return `looper-${Date.now()}-${window.crypto?.randomUUID?.() || Math.random().toString(36).slice(2)}`;
}

export function CloudQuotePage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const orderKey = useRef(idempotencyKey());
  const query = useQuery({ queryKey: ['cloud-quote', id], queryFn: () => api.quoteById(id), enabled: Boolean(id) });
  const prepare = useMutation({
    mutationFn: () => api.prepareOrder(id, orderKey.current),
    onSuccess: order => navigate(`/cloud/orders/${order.id}`, { state: order }),
  });
  if (query.isLoading) return <div className="page narrow-page"><LoadingState /></div>;
  if (query.isError || !query.data) return <div className="page narrow-page"><ErrorState error={query.error} onRetry={() => query.refetch()} /></div>;
  const quote = query.data;
  const purchasable = quote.status === 'valid' && !quote.estimated && Date.parse(quote.expiresAt) > Date.now();
  return <div className="page narrow-page">
    <Link className="back-link" to="/cloud/market">← 返回云资源市场</Link>
    <PageHeader title="报价详情" description={`${quote.provider} · ${quote.id}`} actions={<button className="button secondary" onClick={() => query.refetch()}><RefreshCw size={15} />刷新报价</button>} />
    <section className="order-hero">
      <div className="order-hero-icon"><ClipboardCheck size={24} /></div>
      <div><span className={`order-status large ${quote.status}`}>{quote.estimated ? '估算报价' : quote.status === 'valid' ? '有效报价' : '不可购买'}</span><h2>{quote.spec.instanceName}</h2><p>{quote.spec.instanceType} · {quote.spec.region} · {quote.spec.zone}</p></div>
      <strong className="order-price">{quote.hourlyAmount} {quote.currency}<small>/ 小时</small></strong>
    </section>
    <section className="panel quote-review-panel">
      <div className="panel-heading"><div><h2>绑定规格</h2><p>Spec digest · {quote.specDigest}</p></div></div>
      <div className="fact-grid">
        <div><span>镜像</span><strong>{quote.spec.imageId}</strong></div><div><span>数量</span><strong>{quote.spec.count}</strong></div>
        <div><span>网络</span><strong>{quote.spec.vpcId} / {quote.spec.subnetId}</strong></div><div><span>安全组</span><strong>{quote.spec.securityGroupIds.join(', ')}</strong></div>
        <div><span>系统盘</span><strong>{quote.spec.systemDiskGib} GiB</strong></div><div><span>有效期</span><strong>{new Date(quote.expiresAt).toLocaleString()}</strong></div>
      </div>
    </section>
    {typeof quote.details.warning === 'string' && <div className="notice warning"><div><strong>供应商报价说明</strong><p>{quote.details.warning}</p></div></div>}
    <div className="detail-actions"><button className="button primary" disabled={!purchasable || prepare.isPending} onClick={() => prepare.mutate()}><ShoppingCart size={16} />{quote.estimated ? '估算价不可购买' : quote.status !== 'valid' ? '报价已失效' : prepare.isPending ? '准备订单...' : '进入订单确认'}</button></div>
    {prepare.isError && <div className="inline-error">{prepare.error instanceof Error ? prepare.error.message : '订单准备失败'}</div>}
  </div>;
}
