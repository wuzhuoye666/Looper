import { useMutation, useQueryClient } from '@tanstack/react-query';
import { CheckCircle2, LoaderCircle, PlugZap } from 'lucide-react';
import { api } from '../lib/api';
import type { Target } from '../lib/types';

export function TargetSshButton({
  target,
  compact = false,
  onConfigure,
}: { target: Target; compact?: boolean; onConfigure?: () => void }) {
  const queryClient = useQueryClient();
  const test = useMutation({
    mutationFn: () => api.testTargetSsh(target.id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['targets'] });
    },
  });

  if (target.sshAutomation?.status === 'waiting_endpoint') {
    return <div className="target-ssh-control">
      <span className="ssh-credential-pending">购买密钥已加密保存，等待 IP 后自动测试</span>
    </div>;
  }

  if (!target.credentialsRemembered) {
    if (target.type !== 'external' && onConfigure) {
      return <div className="target-ssh-control">
        <button
          type="button"
          className="button secondary target-ssh-button"
          disabled={target.lifecycleStatus !== 'active'}
          onClick={onConfigure}
          aria-label={target.name + ' · 配置 SSH 并测试'}
        >
          <PlugZap size={14} />配置 SSH 并测试
        </button>
      </div>;
    }
    return <div className="target-ssh-control">
      <span className="ssh-credential-missing" title="先连接一次并保存凭据，后续即可免输入测试">未保存 SSH 凭据</span>
    </div>;
  }

  const label = test.isPending
    ? '正在测试并恢复…'
    : test.isSuccess
      ? 'SSH 已连通'
      : target.runnable
        ? '测试 SSH'
        : '测试并恢复';

  return <div className={`target-ssh-control${compact ? ' compact' : ''}`}>
    <button
      type="button"
      className="button secondary target-ssh-button"
      disabled={test.isPending || target.lifecycleStatus !== 'active'}
      onClick={() => test.mutate()}
      aria-label={`${target.name} · ${label}`}
    >
      {test.isPending ? <LoaderCircle className="spin" size={14}/> : test.isSuccess ? <CheckCircle2 size={14}/> : <PlugZap size={14}/>}
      {label}
    </button>
    {test.isSuccess && <small className="ssh-test-success">凭据已复用，Worker 正在自动上线</small>}
    {test.isError && <small className="ssh-test-error" role="alert">{test.error instanceof Error ? test.error.message : 'SSH 连接失败'}</small>}
  </div>;
}
