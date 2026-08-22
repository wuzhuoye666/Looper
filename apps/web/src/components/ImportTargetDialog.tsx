import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Download, X } from 'lucide-react';
import { useState, type FormEvent } from 'react';
import { api } from '../lib/api';

const emptyDraft = {
  name: '',
  endpoint: '',
  description: '',
  framework: '',
  version: '',
  processor: '',
  logicalCpuCount: '',
  memoryGib: '',
  instanceType: '',
  region: '',
  zone: '',
  capabilities: '',
  runnable: false,
};

export function ImportTargetDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState(emptyDraft);
  const [error, setError] = useState('');
  const set = (key: keyof typeof emptyDraft, value: string | boolean) => {
    setDraft(current => ({ ...current, [key]: value }));
  };

  const submit = useMutation({
    mutationFn: async () => {
      const payload: Record<string, unknown> = {
        name: draft.name.trim(),
        endpoint: draft.endpoint.trim(),
        description: draft.description.trim() || undefined,
        framework: draft.framework.trim() || undefined,
        version: draft.version.trim() || undefined,
        hardware: {
          processor: draft.processor.trim() || undefined,
          logical_cpu_count: draft.logicalCpuCount ? Number(draft.logicalCpuCount) : undefined,
          memory_gib: draft.memoryGib ? Number(draft.memoryGib) : undefined,
          instance_type: draft.instanceType.trim() || undefined,
        },
        location: {
          region: draft.region.trim() || undefined,
          zone: draft.zone.trim() || undefined,
        },
        capabilities: draft.capabilities.split(/[,，]/).map(item => item.trim()).filter(Boolean),
        runnable: draft.runnable,
      };
      return api.importExternalTarget(payload);
    },
    onSuccess: () => {
      setDraft(emptyDraft);
      setError('');
      onClose();
      void queryClient.invalidateQueries({ queryKey: ['targets'] });
    },
    onError: nextError => {
      setError(nextError instanceof Error ? nextError.message : '导入失败');
    },
  });

  if (!open) return null;
  return (
    <div className="operator-overlay" role="presentation" onMouseDown={() => { if (!submit.isPending) onClose(); }}>
      <form
        className="operator-dialog import-target-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="import-target-title"
        onSubmit={(event: FormEvent) => { event.preventDefault(); submit.mutate(); }}
        onMouseDown={event => event.stopPropagation()}
      >
        <div className="operator-dialog-heading">
          <div><span className="eyebrow">EXTERNAL TARGET</span><h2 id="import-target-title">导入外部机器</h2></div>
          <button className="icon-button" type="button" onClick={() => { if (!submit.isPending) onClose(); }} aria-label="关闭"><X size={18} /></button>
        </div>
        <p className="dialog-hint">导入自有/其他云/裸金属机器，作为候选资源参与实验。只登记声明信息，不存储任何凭据；接入由 worker 上报。</p>
        <div className="import-form-grid">
          <label><span>名称 *</span><input value={draft.name} onChange={event => set('name', event.target.value)} placeholder="如 onprem-db-01" /></label>
          <label><span>IP / 主机名 *</span><input value={draft.endpoint} onChange={event => set('endpoint', event.target.value)} placeholder="如 10.0.0.7 或 db-01.internal" /></label>
          <label className="import-span"><span>说明</span><input value={draft.description} onChange={event => set('description', event.target.value)} placeholder="用途、位置等（可选）" /></label>
          <label><span>操作系统 / 框架</span><input value={draft.framework} onChange={event => set('framework', event.target.value)} placeholder="如 Linux / Ubuntu 22.04（可选）" /></label>
          <label><span>版本</span><input value={draft.version} onChange={event => set('version', event.target.value)} placeholder="内核或发行版版本（可选）" /></label>
          <label><span>CPU 型号</span><input value={draft.processor} onChange={event => set('processor', event.target.value)} placeholder="如 EPYC 7B13（可选）" /></label>
          <label><span>vCPU 数 *</span><input type="number" min="1" value={draft.logicalCpuCount} onChange={event => set('logicalCpuCount', event.target.value)} placeholder="如 16" /></label>
          <label><span>内存 GiB *</span><input type="number" min="0.1" step="0.1" value={draft.memoryGib} onChange={event => set('memoryGib', event.target.value)} placeholder="如 64" /></label>
          <label><span>规格型号</span><input value={draft.instanceType} onChange={event => set('instanceType', event.target.value)} placeholder="可选，如 S4.2XLARGE16" /></label>
          <label><span>区域</span><input value={draft.region} onChange={event => set('region', event.target.value)} placeholder="可选" /></label>
          <label><span>可用区</span><input value={draft.zone} onChange={event => set('zone', event.target.value)} placeholder="可选" /></label>
          <label className="import-span"><span>能力标签</span><input value={draft.capabilities} onChange={event => set('capabilities', event.target.value)} placeholder="逗号分隔，可选" /></label>
        </div>
        <label className="import-runnable"><input type="checkbox" checked={draft.runnable} onChange={event => set('runnable', event.target.checked)} />已由 worker 接入（可执行实验）</label>
        {error && <div className="error-banner">{error}</div>}
        <div className="action-row">
          <button className="button" type="button" onClick={() => onClose()} disabled={submit.isPending}>取消</button>
          <button className="button primary" type="submit" disabled={submit.isPending}><Download size={16} />{submit.isPending ? '导入中…' : '导入机器'}</button>
        </div>
      </form>
    </div>
  );
}
