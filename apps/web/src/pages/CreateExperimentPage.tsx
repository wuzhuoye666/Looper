import { useMutation, useQuery } from '@tanstack/react-query';
import { Boxes, Check, ChevronRight, ClipboardCheck, Server } from 'lucide-react';
import { FormEvent, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { BackLink } from '../components/Layout';
import { ErrorState } from '../components/States';
import { api } from '../lib/api';

const steps = [
  { label: '采购问题', icon: ClipboardCheck },
  { label: '场景与资源', icon: Boxes },
  { label: '证据协议', icon: Server },
];

export function CreateExperimentPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [form, setForm] = useState({
    name: '',
    description: '',
    benchmarkKey: '',
    targetIds: [] as string[],
    targetBindings: {} as Record<string, { variantId: string; label: string; placementPairId: string }>,
    repeats: 5,
    timeout: 86400,
    seed: 20260821,
  });
  const benchmarks = useQuery({ queryKey: ['benchmarks'], queryFn: api.benchmarks });
  const targets = useQuery({ queryKey: ['targets'], queryFn: api.targets });
  const scenarios = useMemo(
    () => (benchmarks.data?.items || []).filter(item => item.category === 'scenario'),
    [benchmarks.data],
  );
  const selectedBenchmark = scenarios.find(item => (item.key || item.id) === form.benchmarkKey);
  const mutation = useMutation({
    mutationFn: () => api.createExperiment({
      mode: 'selection',
      name: form.name,
      description: form.description,
      benchmarkId: selectedBenchmark?.id,
      benchmarkVersion: selectedBenchmark?.version,
      targetIds: form.targetIds,
      targetBindings: form.targetIds.map(targetId => ({
        targetId,
        ...form.targetBindings[targetId],
      })),
      config: {
        repeats: Number(form.repeats),
        timeout: Number(form.timeout),
        seed: Number(form.seed),
      },
    }),
    onSuccess: data => navigate(`/experiments/${data.id}`),
  });
  const update = (key: string, value: string | number) => {
    setForm(current => ({ ...current, [key]: value }));
  };
  const toggleTarget = (targetId: string, label: string) => {
    setForm(current => {
      const selected = current.targetIds.includes(targetId);
      const targetBindings = { ...current.targetBindings };
      if (selected) delete targetBindings[targetId];
      else targetBindings[targetId] = {
        variantId: targetId,
        label,
        placementPairId: 'placement-1',
      };
      return {
        ...current,
        targetIds: selected
          ? current.targetIds.filter(id => id !== targetId)
          : [...current.targetIds, targetId],
        targetBindings,
      };
    });
  };
  const updateBinding = (targetId: string, key: 'variantId' | 'placementPairId', value: string) => {
    setForm(current => ({
      ...current,
      targetBindings: {
        ...current.targetBindings,
        [targetId]: { ...current.targetBindings[targetId], [key]: value },
      },
    }));
  };
  const next = (event: FormEvent) => {
    event.preventDefault();
    if (step === 1 && (!selectedBenchmark || form.targetIds.length === 0)) return;
    if (step < 2) setStep(step + 1);
    else mutation.mutate();
  };

  return <div className="page narrow-page">
    <BackLink to="/experiments">返回选型研究</BackLink>
    <header className="workspace-heading">
      <div><h1>新建选型研究</h1><p>绑定采购问题、真实 workload 和候选服务器。</p></div>
    </header>
    <ol className="stepper" aria-label="创建步骤">
      {steps.map((item, index) => <li className={index === step ? 'active' : index < step ? 'done' : ''} key={item.label}>
        <span>{index < step ? <Check size={15} /> : index + 1}</span>
        <div><small>步骤 {index + 1}</small><strong>{item.label}</strong></div>
      </li>)}
    </ol>
    <form className="panel form-panel" onSubmit={next}>
      {step === 0 && <fieldset>
        <legend>采购问题</legend>
        <div className="form-grid form-section-gap">
          <label className="full"><span>研究名称 *</span><input required autoFocus value={form.name} onChange={event => update('name', event.target.value)} placeholder="例如：广州 8 vCPU 数据库实例选型" /></label>
          <label className="full"><span>决策背景</span><textarea rows={4} value={form.description} onChange={event => update('description', event.target.value)} placeholder="记录业务 SLO、预算和适用范围" /></label>
        </div>
      </fieldset>}
      {step === 1 && <fieldset>
        <legend>场景与候选资源</legend>
        <div className="form-grid form-section-gap">
          <label className="full"><span>Benchmark 场景 *</span>
            <select required value={form.benchmarkKey} onChange={event => update('benchmarkKey', event.target.value)}>
              <option value="">选择场景</option>
              {scenarios.map(item => <option key={item.key || `${item.id}-${item.version}`} value={item.key || item.id}>{item.name}{item.version ? ` · ${item.version}` : ''}</option>)}
            </select>
          </label>
          {selectedBenchmark?.scenario && <div className="scenario-facts full">
            <div><span>决策问题</span><strong>{selectedBenchmark.scenario.decision_question}</strong></div>
            <div><span>主指标</span><strong>{selectedBenchmark.primaryMetric}</strong></div>
            <div><span>拓扑</span><strong>{selectedBenchmark.scenario.topology}</strong></div>
            <div><span>执行状态</span><strong>{selectedBenchmark.runnable ? '可执行' : 'Stage 0 · 仅本地契约'}</strong></div>
          </div>}
          <div className="full"><span className="field-label">候选资源 *</span>
            <div className="target-choice-list">
              {(targets.data?.items || []).map(target => <label key={target.id} className={form.targetIds.includes(target.id) ? 'selected' : ''}>
                <input type="checkbox" checked={form.targetIds.includes(target.id)} onChange={() => toggleTarget(target.id, target.name)} />
                <span><strong>{target.name}</strong><small>{target.hardware || target.id}</small></span>
                <em>{target.runnable ? '可运行' : target.status === 'inventory' ? '仅库存' : target.status || '未知'}</em>
              </label>)}
            </div>
            {form.targetIds.length > 0 && <div className="target-binding-editor">
              <div className="binding-header"><span>候选资源</span><span>SKU / Variant</span><span>Placement pair</span></div>
              {form.targetIds.map(targetId => {
                const binding = form.targetBindings[targetId];
                return <div className="binding-row" key={targetId}>
                  <strong>{binding.label}</strong>
                  <label><span className="sr-only">SKU / Variant · {binding.label}</span><input required value={binding.variantId} onChange={event => updateBinding(targetId, 'variantId', event.target.value)} /></label>
                  <label><span className="sr-only">Placement pair · {binding.label}</span><input required value={binding.placementPairId} onChange={event => updateBinding(targetId, 'placementPairId', event.target.value)} /></label>
                </div>;
              })}
            </div>}
            {targets.isError && <small>候选资源加载失败</small>}
          </div>
        </div>
      </fieldset>}
      {step === 2 && <fieldset>
        <legend>证据协议</legend>
        <div className="form-grid form-section-gap">
          <label><span>每个目标重复数</span><input type="number" min="3" max="50" value={form.repeats} onChange={event => update('repeats', Number(event.target.value))} /></label>
          <label><span>研究硬超时（秒）</span><input type="number" min="300" max="31536000" value={form.timeout} onChange={event => update('timeout', Number(event.target.value))} /></label>
          <label><span>随机顺序种子</span><input type="number" min="0" value={form.seed} onChange={event => update('seed', Number(event.target.value))} /></label>
          <label><span>推断单位</span><input value="time_block · placement_pair" readOnly /></label>
        </div>
        <div className="review-strip">
          <div><span>场景</span><strong>{selectedBenchmark?.name || '未选择'}</strong></div>
          <div><span>候选资源</span><strong>{form.targetIds.length} 个 · {new Set(form.targetIds.map(id => form.targetBindings[id]?.placementPairId)).size} 个 placement</strong></div>
          <div><span>状态</span><strong>{selectedBenchmark?.runnable ? '可进入执行队列' : '保存为待执行草稿'}</strong></div>
        </div>
      </fieldset>}
      {mutation.isError && <ErrorState error={mutation.error} />}
      <div className="form-actions">
        {step > 0 && <button type="button" className="button secondary" onClick={() => setStep(step - 1)}>上一步</button>}
        <button className="button primary" disabled={mutation.isPending || (step === 1 && (!selectedBenchmark || form.targetIds.length === 0))}>
          {step < 2 ? <>下一步<ChevronRight size={16} /></> : mutation.isPending ? '正在保存…' : '保存选型研究'}
        </button>
      </div>
    </form>
  </div>;
}
