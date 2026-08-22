import { AlertCircle, Inbox, LoaderCircle, RefreshCw } from 'lucide-react';

export function LoadingState({ label = '正在加载数据' }: { label?: string }) {
  return <div className="state-panel" role="status"><LoaderCircle className="spin" size={22} /><span>{label}</span></div>;
}
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : '加载失败，请稍后重试';
  return <div className="state-panel state-error" role="alert"><AlertCircle size={22} /><div><strong>无法获取数据</strong><p>{message}</p></div>{onRetry && <button className="button secondary" onClick={onRetry}><RefreshCw size={15} />重试</button>}</div>;
}
export function EmptyState({ title = '暂无数据', description = '当前筛选条件下没有可显示的内容。', action }: { title?: string; description?: string; action?: React.ReactNode }) {
  return <div className="state-panel empty"><Inbox size={25} /><div><strong>{title}</strong><p>{description}</p></div>{action}</div>;
}
