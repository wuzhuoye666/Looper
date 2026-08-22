import { useMutation, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, KeyRound, Server, X } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { api } from '../lib/api';
import type { Target } from '../lib/types';

type AuthMethod = 'password' | 'private-key' | 'ssh-agent';

const emptyDraft = {
  endpoint: '',
  port: '22',
  username: '',
  authMethod: 'password' as AuthMethod,
  password: '',
  privateKey: '',
  passphrase: '',
  expectedHostKey: '',
};

export function ImportTargetDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(emptyDraft);
  const [connected, setConnected] = useState<Target | null>(null);
  const [error, setError] = useState('');
  const set = (key: keyof typeof emptyDraft, value: string) => {
    setDraft(current => ({ ...current, [key]: value }));
  };

  const connect = useMutation({
    mutationFn: () => {
      const fingerprint = draft.expectedHostKey.trim();
      return api.connectExternalTarget({
      endpoint: draft.endpoint.trim(),
      port: Number(draft.port),
      username: draft.username.trim(),
      auth_method: draft.authMethod,
      password: draft.authMethod === 'password' ? draft.password : undefined,
      private_key: draft.authMethod === 'private-key' ? draft.privateKey : undefined,
      passphrase: draft.authMethod === 'private-key' && draft.passphrase ? draft.passphrase : undefined,
      expected_host_key_sha256: fingerprint ? (fingerprint.startsWith('SHA256:') ? fingerprint : `SHA256:${fingerprint}`) : undefined,
    });
    },
    onSuccess: target => {
      setConnected(target);
      setError('');
      setDraft(current => ({ ...current, password: '', privateKey: '', passphrase: '' }));
      void queryClient.invalidateQueries({ queryKey: ['targets'] });
    },
    onError: nextError => {
      setError(nextError instanceof Error ? nextError.message : '连接失败');
    },
  });

  const close = () => {
    if (connect.isPending) return;
    setDraft(emptyDraft);
    setConnected(null);
    setError('');
    connect.reset();
    onClose();
  };

  if (!open) return null;
  const fingerprint = connected?.fingerprint;
  return (
    <div className="operator-overlay" role="presentation" onMouseDown={close}>
      <form
        className="operator-dialog import-target-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-target-title"
        onSubmit={(event: FormEvent) => { event.preventDefault(); if (!connected) connect.mutate(); }}
        onMouseDown={event => event.stopPropagation()}
      >
        <div className="operator-dialog-heading">
          <div><span className="eyebrow">EXTERNAL TARGET</span><h2 id="import-target-title">连接外部机器</h2></div>
          <button className="icon-button" type="button" onClick={close} aria-label="关闭"><X size={18} /></button>
        </div>

        {connected ? <>
          <div className="target-discovery-success">
            <CheckCircle2 size={22} />
            <div><strong>连接成功，Worker 已部署</strong><p>机器参数已读取，测试程序会在这台服务器执行；凭据已清除且不会保存。</p></div>
          </div>
          <div className="target-discovery-card">
            <div className="target-discovery-title"><Server size={20} /><div><strong>{connected.name}</strong><code>{connected.endpoint}</code></div></div>
            <dl>
              <div><dt>系统</dt><dd>{connected.framework || '—'}</dd></div>
              <div><dt>内核</dt><dd>{connected.version || '—'}</dd></div>
              <div><dt>CPU</dt><dd>{fingerprint?.processor || '—'}</dd></div>
              <div><dt>vCPU</dt><dd>{fingerprint?.logical_cpu_count ?? '—'}</dd></div>
              <div><dt>内存</dt><dd>{fingerprint?.memory_gib ? `${fingerprint.memory_gib} GiB` : '—'}</dd></div>
              <div><dt>架构</dt><dd>{fingerprint?.architecture || '—'}</dd></div>
            </dl>
            {fingerprint?.host_key_sha256 && <small>SSH 主机指纹 · <code>{fingerprint.host_key_sha256}</code></small>}
          </div>
          <div className="notice compact"><div><strong>正在等待 Worker 上线</strong><p>部署进程 PID：{connected.deployment?.remotePid || '—'}。上线后会自动获得 Benchmark 能力并进入候选资源。</p></div></div>
          <div className="action-row">
            <button className="button primary" type="button" onClick={close}>完成</button>
          </div>
        </> : <>
          <p className="dialog-hint">输入 SSH 连接信息，Looper 会自动读取机器参数、部署测试 Worker，并通过加密隧道回传数据。</p>
          <div className="import-form-grid connection-form-grid">
            <label className="import-span"><span>IP / 主机名 *</span><input required value={draft.endpoint} onChange={event => set('endpoint', event.target.value)} placeholder="如 10.0.0.7 或 db-01.internal" autoFocus /></label>
            <label><span>连接方式 *</span><select value={draft.authMethod} onChange={event => set('authMethod', event.target.value as AuthMethod)}><option value="password">SSH 密码</option><option value="private-key">SSH 私钥</option><option value="ssh-agent">SSH Agent / 服务端密钥</option></select></label>
            <label><span>用户名 *</span><input required value={draft.username} onChange={event => set('username', event.target.value)} placeholder="如 root 或 ubuntu" autoComplete="username" /></label>
            <label><span>SSH 端口 *</span><input required type="number" min="1" max="65535" value={draft.port} onChange={event => set('port', event.target.value)} /></label>
            {draft.authMethod === 'password' && <label><span>SSH 密码 *</span><input required type="password" value={draft.password} onChange={event => set('password', event.target.value)} autoComplete="current-password" /></label>}
            {draft.authMethod === 'private-key' && <>
              <label className="import-span"><span>SSH 私钥 *</span><textarea required value={draft.privateKey} onChange={event => set('privateKey', event.target.value)} placeholder="-----BEGIN OPENSSH PRIVATE KEY-----" autoComplete="off" /></label>
              <label><span>私钥口令</span><input type="password" value={draft.passphrase} onChange={event => set('passphrase', event.target.value)} autoComplete="off" placeholder="没有则留空" /></label>
            </>}
            <label className="import-span"><span>预期主机指纹</span><input value={draft.expectedHostKey} onChange={event => set('expectedHostKey', event.target.value)} placeholder="可选，SHA256:…；填写后不匹配将拒绝连接" /></label>
          </div>
          <p className="credential-note"><KeyRound size={14} />密码、私钥和口令只用于本次连接，不写入数据库。</p>
          {error && <div className="error-banner">{error}</div>}
          <div className="action-row">
            <button className="button" type="button" onClick={close} disabled={connect.isPending}>取消</button>
            <button className="button primary" type="submit" disabled={connect.isPending}><Server size={16} />{connect.isPending ? '正在读取并部署…' : '连接并部署'}</button>
          </div>
        </>}
      </form>
    </div>
  );
}
