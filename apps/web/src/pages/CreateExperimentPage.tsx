import { useMutation, useQuery } from '@tanstack/react-query';
import { Boxes, Check, ChevronRight, ClipboardCheck, Cloud, Server, Sparkles } from 'lucide-react';
import { FormEvent, useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CloudSelectionAdvisor } from '../components/CloudSelectionAdvisor';
import { BackLink } from '../components/Layout';
import { ErrorState } from '../components/States';
import { TargetSshButton } from '../components/TargetSshButton';
import { api } from '../lib/api';
import {
  benchmarkDecisionQuestion, benchmarkDescription, benchmarkMetricLabel, benchmarkName, benchmarkScenario,
  benchmarkSelectionLabel,
  capabilityLabel, inputKindLabel, topologyLabel,
} from '../lib/benchmarkPresentation';
import type { Benchmark, BenchmarkTargetRequirementSummary, CloudInstanceType, CloudProviderId } from '../lib/types';

const steps = [
  { label: '选型需求', icon: ClipboardCheck },
  { label: '测试场景与资源', icon: Boxes },
  { label: '测试参数', icon: Server },
];

const FALLBACK_SELECTION_DEFAULTS = { repeats: 5, timeout: 86400, seed: 20260301 };
const INTERNAL_BENCHMARK_IDS = new Set(['looper.fixture.config-driven', 'looper.demo.compression']);
type AdvisorProvider = Extract<CloudProviderId, 'tencent' | 'alibaba'>;
type AdvisorProviderState = { region: string; zone: string; selection: CloudInstanceType | null };
const ADVISOR_PROVIDERS: Array<{ id: AdvisorProvider; label: string }> = [
  { id: 'tencent', label: '腾讯云 CVM' },
  { id: 'alibaba', label: '阿里云 ECS' },
];

function selectionDefaults(benchmark?: Pick<Benchmark, 'selectionDefaults'>) {
  return benchmark?.selectionDefaults || FALLBACK_SELECTION_DEFAULTS;
}

const OS_LABELS: Record<string, string> = { linux: 'Linux', windows: 'Windows', macos: 'macOS', aix: 'AIX', other: '其他' };
const ARCH_LABELS: Record<string, string> = { x86_64: 'x86_64', aarch64: 'ARM64', ppc64le: 'ppc64le', riscv64: 'RISC-V', other: '其他' };

function requirementLabels(summary?: BenchmarkTargetRequirementSummary): string[] {
  if (!summary) return [];
  const labels: string[] = [];
  if (summary.osFamilies.length) labels.push(`系统：${summary.osFamilies.map(value => OS_LABELS[value] || value).join(' / ')}`);
  if (summary.architectures.length) labels.push(`架构：${summary.architectures.map(value => ARCH_LABELS[value] || value).join(' / ')}`);
  if (summary.minimumLogicalCpus != null) labels.push(`CPU：至少 ${summary.minimumLogicalCpus} 个逻辑核`);
  if (summary.minimumMemoryGiB != null) labels.push(`内存：至少 ${summary.minimumMemoryGiB} GiB`);
  if (summary.capabilities.length) labels.push(`基础能力：${summary.capabilities.map(capabilityLabel).join('、')}`);
  return labels;
}

export function CreateExperimentPage() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [environmentFilter, setEnvironmentFilter] = useState('');
  const [advisorOpen, setAdvisorOpen] = useState(false);
  const [advisorProvider, setAdvisorProvider] = useState<AdvisorProvider>('tencent');
  const [advisorStates, setAdvisorStates] = useState<Record<AdvisorProvider, AdvisorProviderState>>({
    tencent: { region: '', zone: '', selection: null },
    alibaba: { region: '', zone: '', selection: null },
  });
  const { region: advisorRegion, zone: advisorZone, selection: advisorSelection } = advisorStates[advisorProvider];
  const updateAdvisorState = (patch: Partial<AdvisorProviderState>) => {
    setAdvisorStates(current => ({
      ...current,
      [advisorProvider]: { ...current[advisorProvider], ...patch },
    }));
  };
  const [form, setForm] = useState({
    name: '',
    description: '',
    benchmarkKey: '',
    targetIds: [] as string[],
    targetBindings: {} as Record<string, { variantId: string; label: string; placementPairId: string }>,
    inputBindings: {} as Record<string, { reference: string; digest: string }>,
    ...FALLBACK_SELECTION_DEFAULTS,
  });
  const benchmarks = useQuery({ queryKey: ['benchmarks'], queryFn: api.benchmarks });
  const cloudProviders = useQuery({
    queryKey: ['cloud-providers'],
    queryFn: api.providers,
    enabled: advisorOpen,
    staleTime: 30_000,
  });
  const advisorProviderInfo = cloudProviders.data?.items.find(item => item.id === advisorProvider);
  const advisorCatalogAvailable = Boolean(advisorProviderInfo?.credentialsConfigured);
  const advisorRegions = useQuery({
    queryKey: ['cloud-regions', advisorProvider],
    queryFn: () => api.regions(advisorProvider),
    enabled: advisorOpen && advisorCatalogAvailable,
    staleTime: 300_000,
  });
  const advisorZones = useQuery({
    queryKey: ['cloud-zones', advisorProvider, advisorRegion],
    queryFn: () => api.zones(advisorProvider, advisorRegion),
    enabled: advisorOpen && advisorCatalogAvailable && Boolean(advisorRegion),
    staleTime: 300_000,
  });
  const benchmarkOptions = useMemo(
    () => benchmarks.data?.items || [],
    [benchmarks.data],
  );
  const selectableBenchmarks = useMemo(
    () => benchmarkOptions.filter(item =>
      !INTERNAL_BENCHMARK_IDS.has(item.id) && item.singleNodeReady && item.runnable && item.packageReady),
    [benchmarkOptions],
  );
  useEffect(() => {
    if (!selectableBenchmarks.length) return;
    setForm(current => {
      if (selectableBenchmarks.some(item => (item.key || item.id) === current.benchmarkKey)) return current;
      const benchmark = selectableBenchmarks[0];
      return {
        ...current,
        ...selectionDefaults(benchmark),
        benchmarkKey: benchmark.key || benchmark.id,
        targetIds: [],
        targetBindings: {},
        inputBindings: {},
      };
    });
  }, [selectableBenchmarks]);
  const selectedBenchmark = selectableBenchmarks.find(item => (item.key || item.id) === form.benchmarkKey);
  const targetOptions = useQuery({
    queryKey: ['benchmark-target-options', selectedBenchmark?.id, selectedBenchmark?.version],
    queryFn: () => api.benchmarkTargetOptions(selectedBenchmark!.id, selectedBenchmark!.version!),
    enabled: Boolean(selectedBenchmark?.id && selectedBenchmark?.version),
    refetchInterval: 10_000,
  });
  const environments = targetOptions.data?.environments || [];
  const selectedEnvironment = environments.find(environment => environment.id === environmentFilter);
  const visibleTargets = selectedEnvironment?.targets || [];
  const requirements = requirementLabels(targetOptions.data?.nodeGroup.summary);
  useEffect(() => { setEnvironmentFilter(''); }, [form.benchmarkKey]);
  useEffect(() => {
    if (advisorRegion || !advisorRegions.data?.items.length) return;
    const preferred = advisorRegions.data.items.find(item => item.available !== false) || advisorRegions.data.items[0];
    updateAdvisorState({ region: preferred.id });
  }, [advisorProvider, advisorRegion, advisorRegions.data?.items]);
  useEffect(() => {
    if (advisorZone || !advisorZones.data?.items.length) return;
    const preferred = advisorZones.data.items.find(item => item.available !== false) || advisorZones.data.items[0];
    updateAdvisorState({ zone: preferred.id });
  }, [advisorProvider, advisorZone, advisorZones.data?.items]);
  useEffect(() => {
    if (!targetOptions.data || form.targetIds.length === 0) return;
    const targetStillCompatible = targetOptions.data.environments.some(environment =>
      environment.id === environmentFilter
      && environment.targets.some(target => target.id === form.targetIds[0]),
    );
    if (!targetStillCompatible) {
      setForm(current => ({ ...current, targetIds: [], targetBindings: {} }));
    }
  }, [environmentFilter, form.targetIds, targetOptions.data]);
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
      inputBindings: Object.fromEntries(
        (selectedBenchmark?.inputs || [])
          .filter(input => form.inputBindings[input.id]?.reference)
          .map(input => [input.id, {
            kind: input.kind,
            reference: form.inputBindings[input.id].reference,
            digest: form.inputBindings[input.id].digest || undefined,
          }]),
      ),
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
  const selectTarget = (targetId: string, label: string) => {
    setForm(current => ({
      ...current,
      targetIds: [targetId],
      targetBindings: {
        [targetId]: {
        variantId: targetId,
        label,
        placementPairId: 'placement-1',
        },
      },
    }));
  };
  const updateInputBinding = (inputId: string, key: 'reference' | 'digest', value: string) => {
    setForm(current => {
      const binding = current.inputBindings[inputId] || { reference: '', digest: '' };
      return { ...current, inputBindings: { ...current.inputBindings, [inputId]: { ...binding, [key]: value } } };
    });
  };
  const next = (event: FormEvent) => {
    event.preventDefault();
    if (step === 1 && (!selectedBenchmark || form.targetIds.length === 0)) return;
    if (step < 2) setStep(step + 1);
    else mutation.mutate();
  };
  const changeAdvisorRegion = (value: string) => {
    updateAdvisorState({ region: value, zone: '', selection: null });
  };
  const openPurchaseMarket = (instance = advisorSelection) => {
    if (!instance || !advisorRegion) return;
    const params = new URLSearchParams({
      provider: advisorProvider,
      region: advisorRegion,
      instanceType: instance.id,
    });
    if (advisorZone) params.set('zone', advisorZone);
    navigate(`/cloud/market?${params.toString()}`, {
      state: { preselectedInstance: instance },
    });
  };
  const selectAdvisorInstance = (instance: CloudInstanceType | null) => {
    updateAdvisorState({ selection: instance });
  };

  return <div className="page narrow-page">
    <BackLink to="/experiments">返回选型研究</BackLink>
    <header className="workspace-heading">
      <div><h1>新建选型研究</h1><p>填写选型需求，选择真实测试负载和候选服务器。</p></div>
      <button type="button" className={`button advisor-entry-button ${advisorOpen ? 'secondary open' : 'primary'}`} aria-expanded={advisorOpen} aria-controls="research-selection-advisor" onClick={() => setAdvisorOpen(current => !current)}><Sparkles size={15} />{advisorOpen ? '收起选型助手' : '打开选型助手'}</button>
    </header>
    {advisorOpen && <section id="research-selection-advisor" className="research-advisor-section" aria-label="云服务器选型助手">
      <header className="research-advisor-toolbar">
        <div><span className="eyebrow">CLOUD ADVISOR</span><h2>先确定合适规格，再纳入候选资源</h2><p>助手属于选型研究流程；购买页只负责配置和下单。</p></div>
        <div className="research-provider-strip" aria-label="选型助手云厂商">
          {ADVISOR_PROVIDERS.map(item => {
            const info = cloudProviders.data?.items.find(provider => provider.id === item.id);
            return <button type="button" key={item.id} className={advisorProvider === item.id ? 'selected' : ''} onClick={() => setAdvisorProvider(item.id)}><Cloud size={14} /><span>{item.label}</span><i className={`connection-dot ${info?.credentialsConfigured && info.sdkInstalled ? 'ready' : ''}`} /></button>;
          })}
        </div>
      </header>
      {cloudProviders.isError && <ErrorState error={cloudProviders.error} onRetry={() => cloudProviders.refetch()} />}
      {!cloudProviders.isError && <CloudSelectionAdvisor
        key={advisorProvider}
        provider={advisorProvider}
        catalogAvailable={advisorCatalogAvailable}
        regions={advisorRegions.data?.items || []}
        zones={advisorZones.data?.items || []}
        region={advisorRegion}
        zone={advisorZone}
        onRegionChange={changeAdvisorRegion}
        onZoneChange={value => updateAdvisorState({ zone: value, selection: null })}
        selected={advisorSelection}
        onSelect={selectAdvisorInstance}
      />}
      {advisorSelection && <div className="research-advisor-selection"><div><span>已选建议规格</span><strong>{advisorSelection.id} · {advisorSelection.cpu} vCPU / {advisorSelection.memoryGib} GiB</strong><small>{ADVISOR_PROVIDERS.find(item => item.id === advisorProvider)?.label} · {advisorRegion} · {advisorZone || '自动可用区'}</small></div><button type="button" className="button primary" onClick={() => openPurchaseMarket()}>打开购买配置<ChevronRight size={15} /></button></div>}
    </section>}
    {!advisorOpen && <>
    <ol className="stepper" aria-label="创建步骤">
      {steps.map((item, index) => <li className={index === step ? 'active' : index < step ? 'done' : ''} key={item.label}>
        <span>{index < step ? <Check size={15} /> : index + 1}</span>
        <div><small>步骤 {index + 1}</small><strong>{item.label}</strong></div>
      </li>)}
    </ol>
    <form className="panel form-panel" onSubmit={next}>
      {step === 0 && <fieldset>
        <legend>选型需求</legend>
        <div className="form-grid form-section-gap">
          <label className="full"><span>研究名称 *</span><input required autoFocus value={form.name} onChange={event => update('name', event.target.value)} placeholder="例如：广州 8 vCPU 数据库实例选型" /></label>
          <label className="full"><span>选型背景</span><textarea rows={4} value={form.description} onChange={event => update('description', event.target.value)} placeholder="记录业务服务目标、预算和适用范围" /></label>
        </div>
      </fieldset>}
      {step === 1 && <fieldset>
        <legend>测试场景与候选资源</legend>
        <div className="form-grid form-section-gap">
          <label className="full benchmark-selector"><span>想模拟的业务场景 *</span>
            <select required value={form.benchmarkKey} onChange={event => {
              const benchmarkKey = event.currentTarget.value;
              const benchmark = selectableBenchmarks.find(item => (item.key || item.id) === benchmarkKey);
              mutation.reset();
              setForm(current => ({
                ...current,
                ...selectionDefaults(benchmark),
                benchmarkKey,
                targetIds: [],
                targetBindings: {},
                inputBindings: {},
              }));
            }}>
              {!selectableBenchmarks.length && <option value="">暂无可直接运行的测试套件</option>}
              {selectableBenchmarks.map(item => <option key={item.key || `${item.id}-${item.version}`} value={item.key || item.id}>{benchmarkSelectionLabel(item)}</option>)}
            </select>
            <small>选择最接近真实业务的场景，Looper 会用对应负载比较候选服务器。</small>
          </label>
          {selectedBenchmark?.scenario && <div className="scenario-facts full">
            <div><span>测试场景</span><strong>{benchmarkScenario(selectedBenchmark).label} · {benchmarkScenario(selectedBenchmark).detail}</strong></div>
            <div><span>选型目标</span><strong>{benchmarkDecisionQuestion(selectedBenchmark)}</strong></div>
            <div><span>主要参数</span><strong>{benchmarkMetricLabel(selectedBenchmark, selectedBenchmark.primaryMetric)}</strong></div>
            <div><span>部署方式</span><strong>{topologyLabel(selectedBenchmark.scenario.topology)} · {targetOptions.data?.machineCount ?? 1} 台机器</strong></div>
          </div>}
          {selectedBenchmark && <div className="benchmark-suite-content full"><strong>这个测试会做什么</strong><p>{benchmarkDescription(selectedBenchmark)}</p><small>选型时用来回答：{benchmarkDecisionQuestion(selectedBenchmark)}</small></div>}
          {requirements.length > 0 && <div className="benchmark-requirements full"><strong>测试机器要求</strong><div className="tags">{requirements.map(label => <span key={label}>{label}</span>)}</div></div>}
          <div className="full">
            <div className="candidate-toolbar">
              <div>
                <span className="field-label">候选资源 *</span>
                <small className="candidate-selection-count">单机测试套件只能选择 1 台机器</small>
              </div>
              <label className="candidate-environment-filter">
                <span>测试环境</span>
                <select value={environmentFilter} disabled={targetOptions.isLoading || environments.length === 0} onChange={event => {
                  setEnvironmentFilter(event.target.value);
                  setForm(current => ({ ...current, targetIds: [], targetBindings: {} }));
                  mutation.reset();
                }}>
                  <option value="">请选择测试环境</option>
                  {environments.map(environment => <option key={environment.id} value={environment.id}>{environment.label}（{environment.compatibleCount} 台可用）</option>)}
                </select>
              </label>
            </div>
            <div className="target-choice-list">
              {targetOptions.isLoading && <div className="target-choice-empty">正在按测试套件要求检查资源…</div>}
              {targetOptions.isError && <ErrorState error={targetOptions.error} onRetry={() => targetOptions.refetch()}/>}
              {!targetOptions.isLoading && !targetOptions.isError && environments.length === 0 && <div className="target-choice-empty"><strong>没有满足要求的测试资源</strong>{targetOptions.data?.rejectedSummary.slice(0, 3).map(item => <small key={item.code}>{item.message}（{item.count} 台）</small>)}</div>}
              {!targetOptions.isLoading && !targetOptions.isError && environments.length > 0 && !environmentFilter && <div className="target-choice-empty">请先选择测试环境</div>}
              {visibleTargets.map(target => <div key={target.id} className={`target-choice-row ${form.targetIds.includes(target.id) ? 'selected' : ''}`}>
                <label>
                  <input type="radio" name="benchmark-target" checked={form.targetIds.includes(target.id)} onChange={() => selectTarget(target.id, target.name)} />
                  <span><strong>{target.name}</strong><small>{target.hardware || target.id} · <b>{selectedEnvironment?.label}</b></small></span>
                  <em>符合要求 · 可测试</em>
                </label>
                <TargetSshButton target={target} compact/>
              </div>)}
            </div>
          </div>
        </div>
      </fieldset>}
      {step === 2 && <fieldset>
        <legend>测试参数</legend>
        <div className="form-grid form-section-gap">
          {(selectedBenchmark?.inputs || []).map(input => <div className="full input-binding-field" key={input.id}>
            <label><span>{input.id} · {inputKindLabel(input.kind)}{input.required ? ' *' : ''}</span><input required={input.required} type={input.kind === 'secret' ? 'password' : 'text'} value={form.inputBindings[input.id]?.reference || ''} onChange={event => updateInputBinding(input.id, 'reference', event.target.value)} placeholder={input.kind === 'secret' ? '受管密钥引用' : '资源地址或目标设备引用'} /><small>{input.description || '运行前由调度器校验绑定类型；只传递引用，不在测试合同中保存内容。'}</small></label>
            {input.digestRequired && <label><span>SHA-256 摘要 *</span><input required value={form.inputBindings[input.id]?.digest || ''} onChange={event => updateInputBinding(input.id, 'digest', event.target.value)} placeholder="sha256:…" pattern="sha256:[0-9a-f]{64}" /></label>}
          </div>)}
          <label><span>每个目标重复数</span><input type="number" min={Math.min(3, selectionDefaults(selectedBenchmark).repeats)} max="50" value={form.repeats} onChange={event => update('repeats', Number(event.target.value))} /></label>
          <label><span>最长测试时间（秒）</span><input type="number" min="300" max="31536000" value={form.timeout} onChange={event => update('timeout', Number(event.target.value))} /></label>
          <label><span>测试顺序随机种子</span><input type="number" min="0" value={form.seed} onChange={event => update('seed', Number(event.target.value))} /></label>
          <label><span>对比单位</span><input value="时间分块 · 配对对照" readOnly /></label>
        </div>
        <div className="review-strip">
          <div><span>测试套件</span><strong>{selectedBenchmark ? benchmarkName(selectedBenchmark) : '未选择'}</strong></div>
          <div><span>候选资源</span><strong>{form.targetIds.length} 个 · {new Set(form.targetIds.map(id => form.targetBindings[id]?.placementPairId)).size} 个对照组</strong></div>
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
    </>}
  </div>;
}
