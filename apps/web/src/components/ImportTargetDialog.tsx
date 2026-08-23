import { useMutation, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, KeyRound, Server, Upload, X } from 'lucide-react';
import { useEffect, useState, type FormEvent } from 'react';
import { api } from '../lib/api';
import type { Target } from '../lib/types';

type AuthMethod = 'password' | 'private-key' | 'ssh-agent';

const emptyDraft = {
  endpoint: '',
  port: '22',
  username: 'root',
  authMethod: 'private-key' as AuthMethod,
  password: '',
  privateKey: '',
  rememberCredentials: true,
};

export function ImportTargetDialog({
  open,
  onClose,
  target = null,
}: { open: boolean; onClose: () => void; target?: Target | null }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(emptyDraft);
  const [connected, setConnected] = useState<Target | null>(null);
  const [error, setError] = useState('');
  const [sshKeyFileName, setSshKeyFileName] = useState('');
  const set = (key: keyof typeof emptyDraft, value: string) => {
    setDraft(current => ({ ...current, [key]: value }));
  };

  useEffect(() => {
    if (!open) return;
    setDraft(current => ({
      ...emptyDraft,
      endpoint: target?.endpoint && target.endpoint !== '—' ? target.endpoint : current.endpoint,
    }));
  }, [open, target]);

  const connect = useMutation({
    mutationFn: () => {
      const payload = {
        endpoint: draft.endpoint.trim(),
        port: Number(draft.port),
        username: draft.username.trim(),
        auth_method: draft.authMethod,
        password: draft.authMethod === 'password' ? draft.password : undefined,
        private_key: draft.authMethod === 'private-key' ? draft.privateKey : undefined,
        remember_credentials: draft.rememberCredentials,
      };
      return target ? api.connectTargetSsh(target.id, payload) : api.connectExternalTarget(payload);
    },
    onSuccess: target => {
      setConnected(target);
      setError('');
      setDraft(current => ({ ...current, password: '', privateKey: '' }));
      setSshKeyFileName('');
      void queryClient.invalidateQueries({ queryKey: ['targets'] });
    },
    onError: nextError => {
      setError(nextError instanceof Error ? nextError.message : '连接失败');
    },
  });

  const close = () => {
    if (connect.isPending) return;
    setDraft(emptyDraft);
    setSshKeyFileName('');
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
          <div><span className="eyebrow">{target ? 'PURCHASED TARGET' : 'EXTERNAL TARGET'}</span><h2 id="import-target-title">{target ? '配置并测试 SSH' : '连接外部机器'}</h2></div>
          <button className="icon-button" type="button" onClick={close} aria-label="关闭"><X size={18} /></button>
        </div>

        {connected ? <>
          <div className="target-discovery-success">
            <CheckCircle2 size={22} />
            <div><strong>连接成功，Worker 已部署</strong><p>{connected.credentialsRemembered ? 'SSH 凭据已在本机加密保存；后端重启时会校验主机指纹并自动重建隧道。' : '机器参数已读取，但 SSH 凭据未保存，后端重启后需要重新连接。'}</p></div>
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
          <p className="dialog-hint">{target ? `为 ${target.name} 补充 SSH 凭据。连接成功后会读取机器参数、部署 Worker，并保存加密凭据供后续自动测试。` : '输入 SSH 连接信息，Looper 会自动读取机器参数、部署测试 Worker，并通过加密隧道回传数据。'}</p>
          <div className="import-form-grid connection-form-grid">
            <label className="import-span"><span>IP / 主机名 *</span><input required value={draft.endpoint} onChange={event => set('endpoint', event.target.value)} placeholder="如 10.0.0.7 或 db-01.internal" autoFocus /></label>
            <label><span>连接方式 *</span><select value={draft.authMethod} onChange={event => set('authMethod', event.target.value as AuthMethod)}><option value="private-key">SSH 私钥文件</option><option value="password">SSH 密码</option></select></label>
            <label><span>用户名 *</span><input required value={draft.username} onChange={event => set('username', event.target.value)} placeholder="root" autoComplete="username" /></label>
            <label><span>SSH 端口 *</span><input required type="number" min="1" max="65535" value={draft.port} onChange={event => set('port', event.target.value)} /></label>
            {draft.authMethod === 'password' && <label><span>SSH 密码 *</span><input required type="password" value={draft.password} onChange={event => set('password', event.target.value)} autoComplete="current-password" /></label>}
            {draft.authMethod === 'private-key' && <label className="import-span"><span>SSH 私钥文件 *</span><div className="ssh-key-file-picker"><input aria-label="SSH 私钥文件 *" required type="file" accept=".pem,.key,application/x-pem-file,text/plain" onChange={async event => { const file = event.target.files?.[0]; if (!file) return; const reader = new FileReader(); reader.onload = () => setDraft(current => ({ ...current, privateKey: String(reader.result || '') })); reader.readAsText(file); setSshKeyFileName(file.name); }} /><span>{sshKeyFileName || '请选择 .pem 或 .key 文件'}</span><Upload size={15} /></div><small>平台读取文件内容进行 SSH 连接，不上传本地路径。</small></label>}
            <label className="checkbox-field ssh-save-field import-span"><input type="checkbox" checked={draft.rememberCredentials} onChange={event => setDraft(current => ({ ...current, rememberCredentials: event.target.checked }))} /><span>保存密钥 / 密码</span><small>{draft.rememberCredentials ? '连接成功后保存到本机加密凭据仓库。' : '仅本次连接使用，成功后不保存。'}</small></label>
          </div>
          <p className="credential-note"><KeyRound size={14} />密码和私钥不会写入数据库；是否保存由上方开关决定。</p>
          {error && <div className="error-banner">{error}</div>}
          <div className="action-row">
            <button className="button" type="button" onClick={close} disabled={connect.isPending}>取消</button>
            <button className="button primary" type="submit" disabled={connect.isPending || (draft.authMethod === 'private-key' && !draft.privateKey)}><Server size={16} />{connect.isPending ? '正在读取并部署…' : '连接并部署'}</button>
          </div>
        </>}
      </form>
    </div>
  );
}
