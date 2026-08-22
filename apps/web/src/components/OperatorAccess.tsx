import { useQuery, useQueryClient } from '@tanstack/react-query';
import { KeyRound, ShieldCheck, X } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { api, getOperatorToken, setOperatorToken } from '../lib/api';

export function OperatorAccess() {
  const queryClient = useQueryClient();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState('');
  const status = useQuery({
    queryKey: ['cloud-auth-status'],
    queryFn: api.cloudAuthStatus,
    retry: false,
    staleTime: 15_000,
  });
  const authenticated = status.data?.authenticated || false;

  const show = () => {
    setDraft(getOperatorToken());
    setError('');
    setOpen(true);
  };
  const apply = (event: FormEvent) => {
    event.preventDefault();
    const value = draft.trim();
    if (value && value.length < 32) {
      setError('操作员令牌至少需要 32 个字符');
      return;
    }
    setOperatorToken(value);
    void queryClient.invalidateQueries();
    setOpen(false);
  };
  const clear = () => {
    setOperatorToken('');
    setDraft('');
    setError('');
    void queryClient.invalidateQueries();
  };

  return <>
    <button className={`icon-button operator-key ${authenticated ? 'authenticated' : ''}`} type="button" onClick={show} aria-label="操作员访问" title={authenticated ? '操作员已认证' : '操作员访问'}>
      {authenticated ? <ShieldCheck size={17} /> : <KeyRound size={17} />}
    </button>
    {open && <div className="operator-overlay" role="presentation" onMouseDown={() => setOpen(false)}>
      <form className="operator-dialog" role="dialog" aria-modal="true" aria-labelledby="operator-title" onSubmit={apply} onMouseDown={event => event.stopPropagation()}>
        <div className="operator-dialog-heading">
          <div><span className="eyebrow">PURCHASE ACCESS</span><h2 id="operator-title">操作员认证</h2></div>
          <button className="icon-button" type="button" onClick={() => setOpen(false)} aria-label="关闭"><X size={18} /></button>
        </div>
        <label><span>Bearer token</span><input type="password" value={draft} onChange={event => setDraft(event.target.value)} autoFocus autoComplete="off" /></label>
        {error && <div className="error-banner">{error}</div>}
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
  </>;
}
