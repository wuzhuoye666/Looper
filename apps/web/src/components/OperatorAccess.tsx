import { useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound, ShieldCheck, X } from 'lucide-react';
import { createContext, useContext, useEffect, useState, type FormEvent, type ReactNode } from 'react';
import { api, getOperatorToken, OPERATOR_AUTH_INVALID_EVENT, setOperatorToken } from '../lib/api';

type OperatorAccessContextValue = { authenticated: boolean; show: () => void };
export const OPERATOR_ACCESS_CHANGED_EVENT = 'looper:operator-access-changed';
const OperatorAccessContext = createContext<OperatorAccessContextValue | null>(null);

export function OperatorAccessButton() {
  const access = useContext(OperatorAccessContext);
  if (!access) throw new Error('OperatorAccessButton must be rendered inside OperatorAccessProvider');
  return <button className={`icon-button operator-key ${access.authenticated ? 'authenticated' : ''}`} type="button" onClick={access.show} aria-label="操作员访问" title={access.authenticated ? '操作员已认证' : '操作员访问'}>
    {access.authenticated ? <ShieldCheck size={17} /> : <KeyRound size={17} />}
  </button>;
}

export function OperatorAccessProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const [localPending, setLocalPending] = useState(false);
  const status = useQuery({
    queryKey: ['operator-session'],
    queryFn: api.operatorSession,
    retry: false,
    staleTime: 15_000,
  });
  const authenticated = status.data?.authenticated || false;

  useEffect(() => {
    const invalidated = (event: Event) => {
      const message = event instanceof CustomEvent && event.detail?.message
        ? String(event.detail.message) : '操作员会话已失效，请重新认证。';
      setDraft('');
      setError(message);
      setOpen(true);
      void queryClient.invalidateQueries();
    };
    window.addEventListener(OPERATOR_AUTH_INVALID_EVENT, invalidated);
    return () => window.removeEventListener(OPERATOR_AUTH_INVALID_EVENT, invalidated);
  }, [queryClient]);

  const show = () => {
    setDraft(getOperatorToken());
    setError('');
    setOpen(true);
  };
  const apply = async (event: FormEvent) => {
    event.preventDefault();
    const value = draft.trim();
    if (value && value.length < 32) {
      setError('操作员令牌至少需要 32 个字符');
      return;
    }
    setOperatorToken(value);
    try {
      const next = await api.operatorSession();
      if (next.required && !next.authenticated) {
        setOperatorToken('');
        setError('令牌未通过服务端验证');
        return;
      }
      queryClient.setQueryData(['operator-session'], next);
      await queryClient.invalidateQueries();
      window.dispatchEvent(new Event(OPERATOR_ACCESS_CHANGED_EVENT));
      setError('');
      setOpen(false);
    } catch (nextError) {
      setOperatorToken('');
      setError(nextError instanceof Error ? nextError.message : '认证验证失败');
    }
  };
  const clear = () => {
    setOperatorToken('');
    setDraft('');
    setError('');
    window.dispatchEvent(new Event(OPERATOR_ACCESS_CHANGED_EVENT));
    void queryClient.invalidateQueries();
  };
  const localLogin = async () => {
    setLocalPending(true); setError('');
    try {
      const issued = await api.localOperatorSession();
      setOperatorToken(issued.token);
      const next = await api.operatorSession();
      if (!next.authenticated) throw new Error('本机操作员会话验证失败');
      queryClient.setQueryData(['operator-session'], next);
      await queryClient.invalidateQueries();
      window.dispatchEvent(new Event(OPERATOR_ACCESS_CHANGED_EVENT));
      setDraft(''); setOpen(false);
    } catch (nextError) {
      setOperatorToken('');
      setError(nextError instanceof Error ? nextError.message : '本机认证失败');
    } finally { setLocalPending(false); }
  };

  return <OperatorAccessContext.Provider value={{ authenticated, show }}>
    {children}
    {open && <div className="operator-overlay" role="presentation" onMouseDown={() => setOpen(false)}>
      <form className="operator-dialog" role="dialog" aria-modal="true" aria-labelledby="operator-title" onSubmit={apply} onMouseDown={event => event.stopPropagation()}>
        <div className="operator-dialog-heading">
          <div><span className="eyebrow">OPERATOR ACCESS</span><h2 id="operator-title">操作员认证</h2></div>
          <button className="icon-button" type="button" onClick={() => setOpen(false)} aria-label="关闭"><X size={18} /></button>
        </div>
        <label><span>Bearer token</span><input type="password" value={draft} onChange={event => setDraft(event.target.value)} autoFocus autoComplete="off" /></label>
        {error && <div className="error-banner">{error}</div>}
        {status.data?.localBootstrapAvailable && !authenticated && <button className="button primary local-operator-button" type="button" disabled={localPending} onClick={() => void localLogin()}><ShieldCheck size={16}/>{localPending ? '正在建立本机会话…' : '本机一键认证'}</button>}
        <div className="operator-dialog-state">
          <span className={authenticated ? 'status-dot success' : 'status-dot'} />
          {status.data?.required ? authenticated ? '已认证' : '服务器要求认证' : '购买锁未启用'}
        </div>
        <div className="action-row">
          <button className="button" type="button" onClick={clear}>清除</button>
          <button className="button" type="button" onClick={() => setOpen(false)}>取消</button>
          <button className="button primary" type="submit"><KeyRound size={16} />应用令牌</button>
        </div>
      </form>
    </div>}
  </OperatorAccessContext.Provider>;
}
