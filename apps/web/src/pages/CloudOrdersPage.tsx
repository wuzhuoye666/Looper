import { useMutation, useQuery } from '@tanstack/react-query';
import { AlertTriangle, ArrowRight, CheckCircle2, ClipboardList, Copy, Download, ExternalLink, History, KeyRound, RefreshCw, ShieldCheck, TriangleAlert } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';
import type { CloudOrder, CloudOrderEvent } from '../lib/types';

const labels: Record<string, string> = {
  awaiting_confirmation: '待确认', submitting: '提交中', submitted: '已提交', succeeded: '已纳管', failed: '失败', unknown: '结果不明', expired: '已过期',
};
const providers: Record<string, string> = { tencent: '腾讯云 CVM', alibaba: '阿里云 ECS', volcengine: '火山引擎 ECS', baidu: '百度智能云 BCC' };
const eventLabels: Record<string, string> = {
  'cloud.quote.created': '报价快照已创建',
  'cloud.order.awaiting_confirmation': '等待人工确认',
  'cloud.order.confirmation_renewed': '确认窗口已重新签发',
  'cloud.order.price_changed': '价格变化，订单已废止',
  'cloud.order.failed': '供应商明确失败',
  'cloud.order.unknown': '供应商结果不明确',
  'cloud.order.submitted': '实例创建已提交',
  'cloud.order.reconciled': '人工对账已完成',
};
function eventSummary(event: CloudOrderEvent) {
  const payload = event.payload;
  if (payload.resolution) return `${String(payload.resolution)} · ${String(payload.note || '')}`;
  if (payload.amount && payload.currency) return `${String(payload.amount)} ${String(payload.currency)}`;
  if (payload.errorCode) return `${String(payload.errorCode)} · ${String(payload.message || '')}`;
  if (Array.isArray(payload.instanceIds) && payload.instanceIds.length) return payload.instanceIds.join(', ');
  return event.entityType === 'cloud_quote' ? '不可变报价及摘要已持久化' : '订单状态与审计事实已持久化';
}

export function CloudOrdersPage() {
  const { id } = useParams();
  return id ? <OrderDetail id={id} /> : <OrderList />;
}

function OrderList() {
  const [status, setStatus] = useState('');
  const query = useQuery({ queryKey: ['cloud-orders', status], queryFn: () => api.orders(status), refetchInterval: 15_000 });
  return <div className="page"><PageHeader title="云订单" description="报价快照、确认记录、供应商响应和纳管资源的审计轨迹。" actions={<button className="button secondary" onClick={() => query.refetch()}><RefreshCw size={15} />刷新</button>} /><div className="toolbar"><label className="select-field"><ClipboardList size={15} /><select aria-label="订单状态" value={status} onChange={event => setStatus(event.target.value)}><option value="">全部状态</option>{Object.entries(labels).map(([key, value]) => <option key={key} value={key}>{value}</option>)}</select></label><span className="result-count">{query.data?.items.length || 0} 个订单</span></div>{query.isLoading ? <LoadingState /> : query.isError ? <ErrorState error={query.error} onRetry={() => query.refetch()} /> : query.data?.items.length ? <section className="panel table-panel"><div className="table-wrap"><table><thead><tr><th>订单</th><th>云厂商</th><th>资源</th><th>小时金额</th><th>状态</th><th>更新时间</th><th /></tr></thead><tbody>{query.data.items.map(order => <tr key={order.id}><td><Link className="text-link" to={`/cloud/orders/${order.id}`}>{order.id.slice(0, 18)}<ArrowRight size={14} /></Link><span className="cell-meta">{order.spec.instanceName}</span></td><td>{providers[order.provider] || order.provider}</td><td>{order.spec.instanceType}<span className="cell-meta">{order.spec.region} · {order.spec.zone}</span></td><td>{order.hourlyAmount} {order.currency}<span className="cell-meta">按小时</span></td><td><span className={`order-status ${order.status}`}>{labels[order.status] || order.status}</span></td><td>{new Date(order.updatedAt).toLocaleString()}</td><td><Link className="icon-button" to={`/cloud/orders/${order.id}`} aria-label="查看订单"><ExternalLink size={15} /></Link></td></tr>)}</tbody></table></div></section> : <EmptyState title="还没有云订单" action={<Link className="button primary" to="/cloud/market">打开云资源市场</Link>} />}</div>;
}

function OrderDetail({ id }: { id: string }) {
  const location = useLocation();
  const navigate = useNavigate();
  const stateOrder = location.state as CloudOrder | undefined;
  const query = useQuery({ queryKey: ['cloud-order', id], queryFn: () => api.order(id), initialData: stateOrder, refetchInterval: 10_000 });
  const order = query.data;
  const eventsQuery = useQuery({ queryKey: ['cloud-order-events', id], queryFn: () => api.orderEvents(id), refetchInterval: 10_000 });
  const reconciliationContext = useQuery({ queryKey: ['cloud-order-reconciliation', id], queryFn: () => api.orderReconciliationContext(id), enabled: order?.status === 'unknown' });
  const [token, setToken] = useState('');
  const [phrase, setPhrase] = useState('');
  const [amount, setAmount] = useState('');
  const [clock, setClock] = useState(Date.now());
  const [reconcileInstances, setReconcileInstances] = useState('');
  const [reconcileOrderId, setReconcileOrderId] = useState('');
  const [reconcileNote, setReconcileNote] = useState('');
  const [evidencePending, setEvidencePending] = useState(false);
  const [evidenceError, setEvidenceError] = useState('');
  const downloadEvidence = async () => {
    setEvidencePending(true);
    setEvidenceError('');
    try {
      const evidence = await api.orderEvidence(id);
      const blob = new Blob([JSON.stringify(evidence, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = `${id}.cloud-order-evidence.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error) {
      setEvidenceError(error instanceof Error ? error.message : '证据导出失败');
    } finally {
      setEvidencePending(false);
    }
  };
  const confirm = useMutation({ mutationFn: () => api.confirmOrder(id, { confirmationToken: token, acknowledgement: phrase, expectedHourlyAmount: amount }), onSuccess: value => Promise.all([query.refetch(), eventsQuery.refetch()]).then(() => navigate(`/cloud/orders/${value.id}`, { replace: true })) });
  const renewConfirmation = useMutation({
    mutationFn: () => api.renewOrderConfirmation(id),
    onSuccess: value => {
      setToken(value.confirmationToken || '');
      setPhrase('');
      setAmount(value.hourlyAmount);
      setClock(Date.now());
      return Promise.all([query.refetch(), eventsQuery.refetch()]);
    },
  });
  const reconcile = useMutation({
    mutationFn: (resolution: 'submitted' | 'not_created') => api.resolveOrder(id, {
      resolution,
      instanceIds: resolution === 'submitted' ? reconcileInstances.split(',').map(value => value.trim()).filter(Boolean) : [],
      providerOrderId: reconcileOrderId.trim() || undefined,
      note: reconcileNote.trim(),
    }),
    onSuccess: () => Promise.all([query.refetch(), eventsQuery.refetch()]),
  });
  useEffect(() => { if (order) { setToken(order.confirmationToken || ''); setPhrase(''); setAmount(order.hourlyAmount); } }, [order?.id, order?.confirmationToken, order?.hourlyAmount]);
  useEffect(() => {
    if (order?.status !== 'awaiting_confirmation') return;
    const timer = window.setInterval(() => setClock(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [order?.status]);
  if (query.isLoading && !order) return <div className="page"><LoadingState /></div>;
  if (query.isError || !order) return <div className="page"><ErrorState error={query.error} onRetry={() => query.refetch()} /></div>;
  const confirmationRemaining = order.confirmationExpiresAt ? Math.max(0, Date.parse(order.confirmationExpiresAt) - clock) : 0;
  const confirmationExpired = (order.status === 'awaiting_confirmation' && confirmationRemaining === 0) || order.status === 'expired';
  const pending = order.status === 'awaiting_confirmation' && !confirmationExpired;
  return <div className="page narrow-page"><Link className="back-link" to="/cloud/orders">← 返回订单</Link><PageHeader title="订单详情" description={`${providers[order.provider] || order.provider} · ${order.id}`} actions={<div className="action-row"><button className="button secondary" disabled={evidencePending} onClick={downloadEvidence}><Download size={15} />导出证据</button><button className="button secondary" onClick={() => Promise.all([query.refetch(), eventsQuery.refetch()])}><RefreshCw size={15} />刷新状态</button></div>} />{evidenceError && <div className="error-banner">{evidenceError}</div>}<section className="order-hero"><div className="order-hero-icon"><ClipboardList size={24} /></div><div><span className={`order-status large ${order.status}`}>{labels[order.status] || order.status}</span><h2>{order.spec.instanceName}</h2><p>{order.spec.instanceType} · {order.spec.region} · {order.spec.zone}</p></div><strong className="order-price">{order.hourlyAmount} {order.currency}<small>/ 小时</small></strong></section>{order.status === 'unknown' && <><div className="notice danger"><TriangleAlert size={18} /><div><strong>供应商结果不明确</strong><p>不要重新提交购买。先在云厂商控制台或订单 API 中核对实例，再回到 Looper 更新纳管状态。</p></div></div><section className="panel reconciliation-panel"><div className="panel-heading"><div><h2>人工对账</h2><p>仅在云厂商控制台核实后提交，操作会写入审计事件。</p></div><ShieldCheck size={20} /></div>{reconciliationContext.isLoading && <div className="reconciliation-context">正在读取稳定请求标识…</div>}{reconciliationContext.data && <div className="reconciliation-context"><KeyRound size={17} /><div><span>Provider client token</span><code>{reconciliationContext.data.clientToken}</code>{reconciliationContext.data.providerRequestId && <small>Request ID · {reconciliationContext.data.providerRequestId}</small>}</div><button className="icon-button" title="复制 client token" aria-label="复制 client token" onClick={() => navigator.clipboard.writeText(reconciliationContext.data!.clientToken)}><Copy size={15} /></button></div>}{reconciliationContext.isError && <div className="error-banner">{reconciliationContext.error instanceof Error ? reconciliationContext.error.message : '无法读取对账上下文'}</div>}<div className="form-grid"><label><span>供应商订单 ID</span><input value={reconcileOrderId} onChange={event => setReconcileOrderId(event.target.value)} /></label><label><span>实例 ID</span><input value={reconcileInstances} onChange={event => setReconcileInstances(event.target.value)} placeholder="多个用逗号分隔" /></label><label className="full"><span>对账备注 *</span><input value={reconcileNote} onChange={event => setReconcileNote(event.target.value)} /></label></div><div className="action-row"><button className="button secondary" disabled={reconcile.isPending || reconcileNote.trim().length < 8} onClick={() => reconcile.mutate('not_created')}>确认未创建</button><button className="button primary" disabled={reconcile.isPending || reconcileNote.trim().length < 8 || !reconcileInstances.trim()} onClick={() => reconcile.mutate('submitted')}><CheckCircle2 size={16} />标记已创建</button></div>{reconcile.isError && <div className="inline-error">{reconcile.error instanceof Error ? reconcile.error.message : '对账失败'}</div>}</section></>}{order.status === 'failed' && <div className="notice danger"><TriangleAlert size={18} /><div><strong>订单已终止</strong><p>重新购买必须返回云市场获取新报价并创建新订单。</p></div></div>}{confirmationExpired && <div className="notice warning renewal-notice"><AlertTriangle size={18} /><div><strong>确认窗口已过期</strong><p>可以重新验证同一规格和价格并获得新的 30 分钟确认窗口；不会在这一步创建实例。</p>{renewConfirmation.isError && <small className="inline-error">{renewConfirmation.error instanceof Error ? renewConfirmation.error.message : '重新验证失败'}</small>}</div><button className="button primary" disabled={renewConfirmation.isPending} onClick={() => renewConfirmation.mutate()}><RefreshCw size={15} />{renewConfirmation.isPending ? '正在重新询价…' : '重新验证并继续'}</button></div>}{pending && <section className="panel confirmation-panel"><div className="panel-heading"><div><h2>二次确认</h2><p>服务器端将重新询价并校验金额、签名和供应商开关 · 剩余 {Math.ceil(confirmationRemaining / 1000)} 秒</p></div><ShieldCheck size={20} /></div><div className="notice warning"><AlertTriangle size={18} /><div><strong>这一步可能产生真实费用</strong><p>{order.acknowledgement || '确认文本不可用，请重新准备订单。'}</p></div></div><div className="form-grid"><div className="confirmation-token-state"><KeyRound size={17} /><div><span>确认令牌</span><strong>服务端已签发，本页面内有效</strong></div></div><label className="full"><span>原样输入确认文本</span><input value={phrase} onChange={event => setPhrase(event.target.value)} placeholder="输入上方完整确认文本" autoComplete="off" /></label><label><span>预期小时金额</span><input value={amount} onChange={event => setAmount(event.target.value)} inputMode="decimal" /></label></div><button className="button danger-ghost" disabled={confirm.isPending || !token || !phrase} onClick={() => confirm.mutate()}><ShieldCheck size={16} />{confirm.isPending ? '提交中...' : '确认并提交购买'}</button>{confirm.isError && <div className="inline-error">{confirm.error instanceof Error ? confirm.error.message : '购买失败'}</div>}</section>}{order.instanceIds.length > 0 && <section className="panel"><div className="panel-heading"><div><h2>已返回实例</h2><p>创建后已写入 Looper 目标资源，等待下一次 inventory 同步。</p></div><CheckCircle2 size={20} /></div><div className="instance-list">{order.instanceIds.map(instanceId => <div className="instance-row" key={instanceId}><strong>{instanceId}</strong><span>纳管状态：provisioning</span><button className="icon-button" aria-label="复制实例 ID" onClick={() => navigator.clipboard?.writeText(instanceId)}><Copy size={15} /></button></div>)}</div><Link className="button secondary" to="/targets">查看目标资源 <ArrowRight size={15} /></Link></section>}<section className="panel audit-panel"><div className="panel-heading"><div>
<h2>订单事实</h2><p>不可变报价与供应商响应摘要</p></div></div><dl className="fact-grid"><div><dt>Quote digest</dt><dd>{order.quoteDigest}</dd></div><div><dt>Spec digest</dt><dd>{order.specDigest}</dd></div><div><dt>Client token</dt><dd>服务端保管，长度 {order.id ? '64' : '—'} 字符</dd></div><div><dt>创建时间</dt><dd>{new Date(order.createdAt).toLocaleString()}</dd></div><div><dt>供应商订单</dt><dd>{order.providerOrderId || '尚未返回'}</dd></div><div><dt>错误</dt><dd>{order.errorMessage || '无'}</dd></div></dl></section><section className="panel audit-panel"><div className="panel-heading"><div><h2>审计时间线</h2><p>报价、确认、供应商提交和人工恢复事件</p></div><History size={20} /></div>{eventsQuery.isLoading ? <LoadingState /> : eventsQuery.isError ? <ErrorState error={eventsQuery.error} onRetry={() => eventsQuery.refetch()} /> : eventsQuery.data?.items.length ? <ol className="audit-timeline">{eventsQuery.data.items.map(event => <li key={event.id}><span className="audit-marker" /><div><div className="audit-event-heading"><strong>{eventLabels[event.eventType] || event.eventType}</strong><time>{new Date(event.createdAt).toLocaleString()}</time></div><p>{eventSummary(event)}</p><code>{event.id}</code></div></li>)}</ol> : <div className="global-search-empty">尚无审计事件</div>}</section></div>;
}
