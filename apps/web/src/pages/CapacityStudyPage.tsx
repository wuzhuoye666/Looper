import { useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, ArrowLeft, ArrowRight, Check, CheckCircle2, ChevronRight, Circle, Clock3, Code2, Gauge, LoaderCircle, Play, RefreshCw, RotateCcw, Server, ShieldCheck, Square, Trash2 } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { BackLink } from '../components/Layout';
import { PageHeader } from '../components/PageHeader';
import { ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';
import type { CapacityAssertion, CapacityDraft, CapacityPreflight, CapacityScenarioStep, CapacityStudy, Target } from '../lib/types';

const steps = ['审核构建', '业务场景', '设置 SLO', '选择服务器', '预算与确认'];
const activeStatuses = new Set(['queued', 'deploying', 'running', 'resetting', 'cleaning', 'cancelling']);
const phaseLabels: Record<string, string> = { queued: '排队', deploying: '构建与部署', internal: '内网容量', reset: '环境重置', external: '公网容量', cleanup: '清理验证', cancel: '取消', recovery: '中断恢复', 'cleanup-retry': '重新清理' };

function clone<T>(value: T): T { return JSON.parse(JSON.stringify(value)) as T; }
function isWrite(step: CapacityScenarioStep) { return !['GET', 'HEAD', 'OPTIONS'].includes(step.method) || !['none', 'read'].includes(step.sideEffect); }
function firstFrontier(target: { frontiers: Record<string, { confirmed_pass?: number | null; confirmed_fail?: number | null; status: string }> }) { return Object.values(target.frontiers || {})[0]; }
function formatCapacity(value?: number | null) { return value == null ? '未确认' : value.toLocaleString(undefined, { maximumFractionDigits: 2 }); }

function JsonField({ label, value, onChange }: { label: string; value: unknown; onChange: (value: unknown) => void }) {
  const [text, setText] = useState(() => JSON.stringify(value ?? {}, null, 2));
  const [error, setError] = useState('');
  useEffect(() => setText(JSON.stringify(value ?? {}, null, 2)), [value]);
  function commit() {
    try { onChange(JSON.parse(text)); setError(''); }
    catch { setError('JSON 格式无效，尚未保存'); }
  }
  return <label className="capacity-json-field"><span>{label}</span><textarea rows={5} value={text} onChange={event => setText(event.target.value)} onBlur={commit}/>{error && <small className="field-error">{error}</small>}</label>;
}

export function CapacityStudyPage() {
  const { studyId = '' } = useParams();
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey: ['capacity-study', studyId], queryFn: () => api.capacityStudy(studyId),
    refetchInterval: current => activeStatuses.has(current.state.data?.status || '') ? 3000 : false,
  });
  const targetsQuery = useQuery({ queryKey: ['targets', 'capacity'], queryFn: () => api.targets(false) });
  const [draft, setDraft] = useState<CapacityDraft | null>(null);
  const [step, setStep] = useState(0);
  const [revision, setRevision] = useState(0);
  const [preflight, setPreflight] = useState<CapacityPreflight>({});
  const [selectedStepId, setSelectedStepId] = useState('');
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [actionError, setActionError] = useState('');
  const [actionPending, setActionPending] = useState('');
  const [acknowledgePartial, setAcknowledgePartial] = useState(false);
  const [tab, setTab] = useState<'leadership' | 'engineering' | 'audit'>('leadership');
  const initialized = useRef('');
  const latestDraft = useRef<CapacityDraft | null>(null);
  const latestStep = useRef(0);
  const revisionRef = useRef(0);
  const savedDigest = useRef('');
  const saveTimer = useRef<ReturnType<typeof setTimeout>>();
  const saveChain = useRef<Promise<void>>(Promise.resolve());

  useEffect(() => {
    const study = query.data;
    if (!study) return;
    if (initialized.current !== study.id) {
      initialized.current = study.id;
      const next = clone(study.draft);
      setDraft(next); latestDraft.current = next;
      setStep(study.currentStep); latestStep.current = study.currentStep;
      setRevision(study.revision); revisionRef.current = study.revision;
      setPreflight(study.preflight || {});
      setSelectedStepId(next.scenario.steps[0]?.id || '');
      savedDigest.current = JSON.stringify({ draft: next, step: study.currentStep });
    } else if (study.status !== 'draft') {
      setPreflight(study.preflight || {});
    }
  }, [query.data]);

  function enqueueSave(nextDraft = latestDraft.current, nextStep = latestStep.current) {
    if (!nextDraft || query.data?.status !== 'draft') return saveChain.current;
    const digest = JSON.stringify({ draft: nextDraft, step: nextStep });
    if (digest === savedDigest.current) return saveChain.current;
    saveChain.current = saveChain.current.then(async () => {
      const currentDraft = latestDraft.current;
      const currentStep = latestStep.current;
      if (!currentDraft) return;
      const currentDigest = JSON.stringify({ draft: currentDraft, step: currentStep });
      if (currentDigest === savedDigest.current) return;
      setSaving(true); setSaveError('');
      try {
        const updated = await api.updateCapacityStudy(studyId, revisionRef.current, currentStep, currentDraft);
        revisionRef.current = updated.revision; setRevision(updated.revision);
        savedDigest.current = currentDigest; setPreflight(updated.preflight || {});
        queryClient.setQueryData(['capacity-study', studyId], updated);
      } catch (reason) { setSaveError(reason instanceof Error ? reason.message : '自动保存失败'); throw reason; }
      finally { setSaving(false); }
    }).catch(() => undefined);
    return saveChain.current;
  }

  function scheduleSave(nextDraft: CapacityDraft, nextStep = latestStep.current) {
    latestDraft.current = nextDraft; latestStep.current = nextStep;
    if (saveTimer.current) clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(() => { void enqueueSave(); }, 650);
  }

  function changeDraft(mutator: (value: CapacityDraft) => void) {
    if (!draft) return;
    const next = clone(draft); mutator(next); setDraft(next); setPreflight({}); scheduleSave(next);
  }

  function changeStep(next: number) {
    setStep(next); latestStep.current = next;
    if (draft) scheduleSave(draft, next);
  }

  async function flushSave() {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    await enqueueSave();
  }

  async function preflightRun() {
    setActionPending('preflight'); setActionError('');
    try { await flushSave(); const updated = await api.preflightCapacityStudy(studyId); setPreflight(updated.preflight); queryClient.setQueryData(['capacity-study', studyId], updated); }
    catch (reason) { setActionError(reason instanceof Error ? reason.message : '预检失败'); }
    finally { setActionPending(''); }
  }

  async function repairBuild() {
    setActionPending('build-repair'); setActionError('');
    try {
      await flushSave();
      const updated = await api.repairCapacityBuild(studyId, revisionRef.current);
      const next = clone(updated.draft);
      setDraft(next); latestDraft.current = next;
      setStep(updated.currentStep); latestStep.current = updated.currentStep;
      setRevision(updated.revision); revisionRef.current = updated.revision;
      setPreflight(updated.preflight || {});
      savedDigest.current = JSON.stringify({ draft: next, step: updated.currentStep });
      queryClient.setQueryData(['capacity-study', studyId], updated);
    } catch (reason) { setActionError(reason instanceof Error ? reason.message : 'Agent 修复失败'); }
    finally { setActionPending(''); }
  }

  async function startRun() {
    setActionPending('start'); setActionError('');
    try {
      await flushSave();
      const failed = preflight.failedSutIds || [];
      const updated = await api.startCapacityStudy(studyId, revisionRef.current, failed, acknowledgePartial);
      queryClient.setQueryData(['capacity-study', studyId], updated);
    } catch (reason) { setActionError(reason instanceof Error ? reason.message : '启动失败'); }
    finally { setActionPending(''); }
  }

  async function action(kind: 'cancel' | 'cleanup') {
    setActionPending(kind); setActionError('');
    try {
      const updated = kind === 'cancel' ? await api.cancelCapacityStudy(studyId) : await api.retryCapacityCleanup(studyId);
      queryClient.setQueryData(['capacity-study', studyId], updated);
    } catch (reason) { setActionError(reason instanceof Error ? reason.message : '操作失败'); }
    finally { setActionPending(''); }
  }

  if (query.isError) return <div className="page"><ErrorState error={query.error} onRetry={() => query.refetch()}/></div>;
  if (query.isLoading || !query.data || !draft) return <div className="page"><LoadingState label="正在读取容量测试"/></div>;
  const study = query.data;
  if (study.status !== 'draft') return <CapacityRunView study={study} tab={tab} setTab={setTab} pending={actionPending} error={actionError} onCancel={() => void action('cancel')} onCleanup={() => void action('cleanup')} onRefresh={() => void query.refetch()}/>;
  const targets = targetsQuery.data?.items || [];
  const selected = draft.scenario.steps.find(item => item.id === selectedStepId) || draft.scenario.steps[0];
  const writeSteps = draft.scenario.steps.filter(isWrite);
  const buildReady = draft.build.approved && !draft.build.unresolved.length;
  const scenarioReady = Boolean(draft.scenario.steps.length) && (!writeSteps.length || draft.scenario.resetStrategy !== 'none');
  const targetReady = Boolean(draft.targets.sutIds.length && draft.targets.internalLoadGeneratorId && draft.targets.externalLoadGeneratorId) && !draft.targets.sutIds.includes(draft.targets.internalLoadGeneratorId) && !draft.targets.sutIds.includes(draft.targets.externalLoadGeneratorId) && draft.targets.sutIds.every(id => draft.targets.internalBaseUrls[id] && draft.targets.externalBaseUrls[id]);
  const stepReady = [buildReady, scenarioReady, true, targetReady, false];
  const failedSuts = preflight.failedSutIds || [];
  const preflightCurrent = preflight.draftRevision === revision;
  const canStart = preflightCurrent && !(preflight.generatorFailures || []).length && (!failedSuts.length || acknowledgePartial);

  return <div className="page capacity-wizard-page">
    <BackLink to="/capacity">返回容量测试</BackLink>
    <PageHeader title={study.name} description={`自动保存草稿 · 修订 ${revision} · 源码 ${study.sourceArchive.status === 'retained' ? '已加密保留' : '已过期'}`} actions={<span className={`autosave-state ${saveError ? 'error' : ''}`}>{saving ? <><LoaderCircle className="spin" size={14}/>保存中</> : saveError ? <><AlertTriangle size={14}/>{saveError}</> : <><CheckCircle2 size={14}/>已保存</>}</span>}/>
    {study.error?.code === 'capacity_build_validation_failed' && <div className="inline-alert"><AlertTriangle size={15}/><span>远程脚本已经真实构建并自动清理。请在第一步根据日志修复后重试。</span></div>}
    <ol className="capacity-stepper" aria-label="容量测试创建步骤">{steps.map((label, index) => <li key={label} className={index === step ? 'active' : index < step ? 'done' : ''}><button type="button" disabled={index > step} onClick={() => changeStep(index)}><span>{index < step ? <Check size={14}/> : index + 1}</span>{label}</button></li>)}</ol>
    {step === 0 && (
      <BuildStep draft={draft} sourceAvailable={study.sourceArchive.status === 'retained'} pending={actionPending === 'build-repair'} onRepair={() => void repairBuild()} onChange={changeDraft}/>
    )}
    {step === 1 && (
      <ScenarioStepEditor draft={draft} selected={selected} selectedId={selectedStepId} setSelectedId={setSelectedStepId} onChange={changeDraft}/>
    )}
    {step === 2 && (
      <SloStep draft={draft} onChange={changeDraft}/>
    )}
    {step === 3 && (
      <TargetsStep draft={draft} targets={targets} onChange={changeDraft}/>
    )}
    {step === 4 && (
      <ReviewStep study={study} draft={draft} revision={revision} targets={targets} preflight={preflight} pending={actionPending} acknowledge={acknowledgePartial} setAcknowledge={setAcknowledgePartial} onChange={changeDraft} onPreflight={() => void preflightRun()} onStart={() => void startRun()}/>
    )}
    {actionError && <div className="inline-alert"><AlertTriangle size={15}/>{actionError}</div>}
    <div className="capacity-wizard-actions">{step > 0 && <button className="button secondary" type="button" onClick={() => changeStep(step - 1)}><ArrowLeft size={15}/>上一步</button>}<span/>{step < 4 && <button className="button primary" type="button" disabled={!stepReady[step]} onClick={() => changeStep(step + 1)}>下一步<ChevronRight size={16}/></button>}</div>
  </div>;
}

function BuildStep({ draft, sourceAvailable, pending, onRepair, onChange }: { draft: CapacityDraft; sourceAvailable: boolean; pending: boolean; onRepair: () => void; onChange: (mutator: (draft: CapacityDraft) => void) => void }) {
  const blocked = draft.build.unresolved.length > 0;
  return <section className="capacity-wizard-panel"><div className="capacity-step-heading"><div><span>STEP 1</span><h2>脚本检查构建方案</h2><p>脚本先检查源码根目录、Compose、安全策略和数据库迁移；只有真实失败项才交给 Agent。</p></div><span className={`status ${draft.build.approved ? 'status-completed' : blocked ? 'status-failed' : 'status-draft'}`}><span/>{draft.build.approved ? '已确认' : blocked ? '待脚本诊断' : '静态检查通过'}</span></div>
    {blocked ? <div className="capacity-agent-repair"><div className="capacity-agent-repair-heading"><span><AlertTriangle size={19}/></span><div><strong>当前有 {draft.build.unresolved.length} 个待验证问题</strong><p>脚本会先自动修正目录和迁移顺序；仍然失败时，Agent 只接收结构化失败日志，最多修复一次。</p></div></div><ol>{draft.build.unresolved.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ol><button className="button primary" type="button" disabled={pending || !sourceAvailable} onClick={onRepair}>{pending ? <LoaderCircle className="spin" size={16}/> : <RefreshCw size={16}/>} {pending ? '脚本正在检查…' : '运行脚本诊断并修复'}</button><small>{sourceAvailable ? '静态脚本不执行上传代码；只有脚本仍失败时才把必要源码片段与失败项发送给 DeepSeek。' : '加密源码已过期，请先回到接口发现记录重新上传同摘要源码包。'}</small></div> : <div className="capacity-agent-ready"><CheckCircle2 size={20}/><div><strong>静态脚本检查通过</strong><p>选择服务器后会执行真实 Compose 构建、健康检查和 HTTP 冒烟测试；失败会自动清理并返回这里。</p></div></div>}
    {draft.build.checks?.length ? <div className="capacity-review-constraints">{draft.build.checks.map(check => <div key={check.id} className={check.status === 'fail' ? 'fail' : 'pass'}><span>{check.status === 'fail' ? <AlertTriangle size={16}/> : <CheckCircle2 size={16}/>}</span><div><strong>{check.label}{check.status === 'fixed' ? ' · 已自动修正' : ''}</strong><small>{check.detail}</small></div></div>)}</div> : null}
    <div className="capacity-build-summary"><div><small>服务端口</small><strong>{draft.build.servicePort}</strong></div><div><small>健康检查</small><code>{draft.build.healthPath}</code></div><div><small>启动命令</small><code>{draft.build.startCommand}</code></div></div>
    {draft.build.advisories?.length ? <details className="capacity-evidence"><summary>后续运行与场景提示 · {draft.build.advisories.length} 项</summary>{draft.build.advisories.map((item, index) => <code key={`${index}-${item}`}>{item}</code>)}</details> : null}
    <details className="capacity-advanced capacity-build-advanced"><summary><Code2 size={14}/>高级：查看构建文件</summary><p>以下文件只读展示。阻断项只能通过脚本复核；脚本确认失败后才调用 Agent，不能手动清空。</p><div className="capacity-code-grid"><div><strong><Code2 size={15}/>Dockerfile</strong><pre className="capacity-code-preview">{draft.build.dockerfile}</pre></div><div><strong><Code2 size={15}/>compose.capacity.yaml</strong><pre className="capacity-code-preview">{draft.build.compose}</pre></div></div></details>
    <details className="capacity-evidence"><summary>查看 Agent 源码证据</summary>{draft.build.evidence.map(item => <code key={`${item.file}:${item.startLine}`}>{item.file}:{item.startLine}-{item.endLine}</code>)}</details>
    {!blocked && <button className="button primary capacity-build-approve" type="button" disabled={pending || draft.build.approved} onClick={() => onChange(next => { next.build.approved = true; })}><ShieldCheck size={16}/>{draft.build.approved ? '构建方案已确认' : '确认构建方案'}</button>}
  </section>;
}

function ScenarioStepEditor({ draft, selected, selectedId, setSelectedId, onChange }: { draft: CapacityDraft; selected?: CapacityScenarioStep; selectedId: string; setSelectedId: (id: string) => void; onChange: (mutator: (draft: CapacityDraft) => void) => void }) {
  function update(mutator: (step: CapacityScenarioStep) => void) { if (!selected) return; onChange(next => { const item = next.scenario.steps.find(value => value.id === selected.id); if (item) mutator(item); }); }
  function move(offset: number) { if (!selected) return; onChange(next => { const index = next.scenario.steps.findIndex(item => item.id === selected.id); const destination = index + offset; if (index < 0 || destination < 0 || destination >= next.scenario.steps.length) return; [next.scenario.steps[index], next.scenario.steps[destination]] = [next.scenario.steps[destination], next.scenario.steps[index]]; }); }
  function remove() { if (!selected) return; onChange(next => { next.scenario.steps = next.scenario.steps.filter(item => item.id !== selected.id); }); const remaining = draft.scenario.steps.filter(item => item.id !== selected.id); setSelectedId(remaining[0]?.id || ''); }
  return <section className="capacity-wizard-panel"><div className="capacity-step-heading"><div><span>STEP 2</span><h2>编排一次完整业务迭代</h2><p>接口按左到右执行；一次链路全部通过才计为成功业务容量。</p></div><span>{draft.scenario.steps.length} 个接口</span></div>
    <div className="scenario-workbench"><div className="scenario-canvas" aria-label="业务流程链">{draft.scenario.steps.map((item, index) => <div className="scenario-node-wrap" key={item.id}><button type="button" className={`scenario-node ${selectedId === item.id ? 'active' : ''}`} onClick={() => setSelectedId(item.id)}><small>{index + 1}</small><strong>{item.label}</strong><code>{item.method} {item.path}</code>{isWrite(item) && <span>写操作</span>}</button>{index < draft.scenario.steps.length - 1 && <ArrowRight size={18}/>}</div>)}</div>
      {selected ? <aside className="scenario-drawer"><div className="scenario-drawer-heading"><div><small>接口节点</small><strong>{selected.label}</strong></div><div><button className="icon-button" type="button" onClick={() => move(-1)} aria-label="前移"><ArrowLeft size={15}/></button><button className="icon-button" type="button" onClick={() => move(1)} aria-label="后移"><ArrowRight size={15}/></button><button className="icon-button danger" type="button" onClick={remove} aria-label="删除节点"><Trash2 size={15}/></button></div></div>
        <label><span>步骤名称</span><input value={selected.label} onChange={event => update(item => { item.label = event.target.value; })}/></label><div className="capacity-inline-fields"><label><span>方法</span><select value={selected.method} onChange={event => update(item => { item.method = event.target.value; })}>{['GET','POST','PUT','PATCH','DELETE','HEAD'].map(method => <option key={method}>{method}</option>)}</select></label><label><span>路径</span><input value={selected.path} onChange={event => update(item => { item.path = event.target.value; })}/></label></div>
        <JsonField label="请求头（敏感值使用 secret://别名）" value={selected.headers} onChange={value => update(item => { item.headers = value as Record<string, string>; })}/><JsonField label="JSON 请求体" value={selected.body ?? {}} onChange={value => update(item => { item.body = value; })}/><JsonField label="响应变量提取（变量名: JSON 路径）" value={selected.extract} onChange={value => update(item => { item.extract = value as Record<string, string>; })}/>
        <label><span>期望 HTTP 状态码</span><input type="number" value={Number(selected.assertions.find(item => item.kind === 'status')?.expected || 200)} onChange={event => update(item => { const assertion = item.assertions.find(value => value.kind === 'status'); if (assertion) assertion.expected = Number(event.target.value); else item.assertions.unshift({ kind: 'status', field: '', expected: Number(event.target.value) }); })}/></label>
        <JsonField label="JSON 正确性断言（json-equals / json-exists 数组）" value={selected.assertions.filter(item => item.kind !== 'status')} onChange={value => update(item => { const status = item.assertions.filter(assertion => assertion.kind === 'status'); const correctness = Array.isArray(value) ? value.filter(assertion => assertion && typeof assertion === 'object' && ['json-equals', 'json-exists'].includes(String((assertion as Record<string, unknown>).kind))) as CapacityAssertion[] : []; item.assertions = [...status, ...correctness]; })}/>
      </aside> : <aside className="scenario-drawer empty">选择一个接口节点</aside>}
    </div>
    <div className="scenario-reset"><div><strong>测试数据重置</strong><p>{draft.scenario.steps.filter(isWrite).length ? `检测到 ${draft.scenario.steps.filter(isWrite).length} 个写操作，必须配置重置。` : '当前链路只有只读操作。'}</p></div><select value={draft.scenario.resetStrategy} onChange={event => onChange(next => { next.scenario.resetStrategy = event.target.value as CapacityDraft['scenario']['resetStrategy']; })}><option value="none">无需重置</option><option value="compose-recreate">内外网之间重建 Compose 与数据卷</option><option value="custom">运行自定义重置命令</option></select>{draft.scenario.resetStrategy === 'custom' && <input value={draft.scenario.resetCommand} onChange={event => onChange(next => { next.scenario.resetCommand = event.target.value; })} placeholder="单行重置命令"/>}</div>
  </section>;
}

function SloStep({ draft, onChange }: { draft: CapacityDraft; onChange: (mutator: (draft: CapacityDraft) => void) => void }) {
  const fields: Array<[string, keyof CapacityDraft['slo'], number, number, number]> = [['最低业务成功率', 'minimumSuccessRate', 0, 1, 0.0001], ['最大错误率', 'maximumErrorRate', 0, 1, 0.0001], ['最大超时率', 'maximumTimeoutRate', 0, 1, 0.0001], ['P99 上限 (ms)', 'p99Ms', 1, 600000, 1], ['P99.9 上限 (ms)', 'p999Ms', 1, 600000, 1], ['最小尾延迟样本', 'minimumSamples', 100, 10000000, 100]];
  return <section className="capacity-wizard-panel"><div className="capacity-step-heading"><div><span>STEP 3</span><h2>声明容量成立的 SLO</h2><p>容量不是最大并发，而是同时通过正确性、错误、超时和尾延迟门槛的最高确认负载。</p></div><span className="capacity-unit">成功业务迭代 / 秒</span></div><div className="slo-grid">{fields.map(([label, field, min, max, increment]) => <label key={field}><span>{label}</span><input type="number" min={min} max={max} step={increment} value={draft.slo[field]} onChange={event => onChange(next => { (next.slo as unknown as Record<string, number>)[field] = Number(event.target.value); })}/></label>)}</div><details className="capacity-advanced"><summary>高级统计参数</summary><dl><div><dt>置信水平</dt><dd>95% 单侧</dd></div><div><dt>边界判定</dt><dd>5 个 block 至少 4 个通过</dd></div><div><dt>搜索精度</dt><dd>容量区间宽度 ≤ 2.5%</dd></div></dl></details></section>;
}

function TargetsStep({ draft, targets, onChange }: { draft: CapacityDraft; targets: Target[]; onChange: (mutator: (draft: CapacityDraft) => void) => void }) {
  const available = targets.filter(item => item.lifecycleStatus !== 'archived' && item.lifecycleStatus !== 'missing');
  return <section className="capacity-wizard-panel"><div className="capacity-step-heading"><div><span>STEP 4</span><h2>选择被测服务器和施压机</h2><p>施压机负责实际 HTTP 请求，不能与任何被测服务器相同。</p></div><span>{draft.targets.sutIds.length} 台被测机</span></div><div className="capacity-target-layout"><div><h3>被测服务器</h3><div className="capacity-target-options">{available.map(target => <label key={target.id} className={`capacity-target-option ${draft.targets.sutIds.includes(target.id) ? 'selected' : ''}`}><input type="checkbox" checked={draft.targets.sutIds.includes(target.id)} onChange={event => onChange(next => { next.targets.sutIds = event.target.checked ? [...next.targets.sutIds, target.id] : next.targets.sutIds.filter(id => id !== target.id); if (!event.target.checked) { delete next.targets.internalBaseUrls[target.id]; delete next.targets.externalBaseUrls[target.id]; } })}/><Server size={18}/><span><strong>{target.name}</strong><small>{target.hardware || target.endpoint} · {target.runnable ? 'Worker 在线' : 'Worker 未就绪'}</small></span></label>)}</div></div><div className="capacity-generator-fields"><label><span>内网施压机</span><select value={draft.targets.internalLoadGeneratorId} onChange={event => onChange(next => { next.targets.internalLoadGeneratorId = event.target.value; })}><option value="">请选择</option>{available.filter(item => item.runnable && !draft.targets.sutIds.includes(item.id)).map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label><label><span>公网施压机</span><select value={draft.targets.externalLoadGeneratorId} onChange={event => onChange(next => { next.targets.externalLoadGeneratorId = event.target.value; })}><option value="">请选择</option>{available.filter(item => item.runnable && !draft.targets.sutIds.includes(item.id)).map(item => <option value={item.id} key={item.id}>{item.name}</option>)}</select></label></div></div>
    {draft.targets.sutIds.length > 0 && <div className="table-wrap capacity-endpoint-table"><table><thead><tr><th>被测服务器</th><th>内网 URL</th><th>公网 URL</th></tr></thead><tbody>{draft.targets.sutIds.map(id => { const target = available.find(item => item.id === id); return <tr key={id}><td><strong>{target?.name || id}</strong><span className="cell-meta">服务端口 {draft.build.servicePort}</span></td><td><input aria-label={`${target?.name || id} 内网 URL`} value={draft.targets.internalBaseUrls[id] || ''} onChange={event => onChange(next => { next.targets.internalBaseUrls[id] = event.target.value; })} placeholder={`http://内网地址:${draft.build.servicePort}`}/></td><td><input aria-label={`${target?.name || id} 公网 URL`} value={draft.targets.externalBaseUrls[id] || ''} onChange={event => onChange(next => { next.targets.externalBaseUrls[id] = event.target.value; })} placeholder={`http://公网地址:${draft.build.servicePort}`}/></td></tr>; })}</tbody></table></div>}
  </section>;
}

function ReviewStep({ study, draft, revision, targets, preflight, pending, acknowledge, setAcknowledge, onChange, onPreflight, onStart }: { study: CapacityStudy; draft: CapacityDraft; revision: number; targets: Target[]; preflight: CapacityPreflight; pending: string; acknowledge: boolean; setAcknowledge: (value: boolean) => void; onChange: (mutator: (draft: CapacityDraft) => void) => void; onPreflight: () => void; onStart: () => void }) {
  const name = (id: string) => targets.find(item => item.id === id)?.name || id;
  const failed = preflight.failedSutIds || [];
  const current = preflight.draftRevision === revision;
  function budget(field: keyof CapacityDraft['budget'], value: number) { onChange(next => { next.budget[field] = value; }); setAcknowledge(false); }
  return <section className="capacity-wizard-panel"><div className="capacity-step-heading"><div><span>STEP 5</span><h2>预算、执行矩阵与启动确认</h2><p>保存全部配置后执行真实 SSH、Docker 和 Worker 预检。</p></div><span>Operator Access 启动</span></div><div className="capacity-budget-grid"><label><span>最长时间（秒）</span><input type="number" min={120} value={draft.budget.maxSeconds} onChange={event => budget('maxSeconds', Number(event.target.value))}/></label><label><span>最多探测次数</span><input type="number" min={1} value={draft.budget.maxAttempts} onChange={event => budget('maxAttempts', Number(event.target.value))}/></label><label><span>增量费用上限（CNY）</span><input type="number" min={0} step="0.01" value={draft.budget.costCap} onChange={event => budget('costCap', Number(event.target.value))}/></label><label><span>参考负载（迭代/秒）</span><input type="number" min={1} value={draft.budget.referenceRps} onChange={event => budget('referenceRps', Number(event.target.value))}/></label><label><span>单点测量时间（秒）</span><input type="number" min={5} value={draft.budget.measurementSeconds} onChange={event => budget('measurementSeconds', Number(event.target.value))}/></label></div><p className="capacity-budget-edit-note">修改预算会使既有预检失效；保存后需要重新预检。当前只使用已登记服务器，不创建云资源，所以增量费用为 0；既有服务器租金不作无依据推算。</p>
    <div className="table-wrap"><table><thead><tr><th>被测服务器</th><th>内网施压机</th><th>公网施压机</th><th>执行顺序</th></tr></thead><tbody>{draft.targets.sutIds.map(id => <tr key={id} className={failed.includes(id) ? 'row-failed' : ''}><td>{name(id)}</td><td>{name(draft.targets.internalLoadGeneratorId)}</td><td>{name(draft.targets.externalLoadGeneratorId)}</td><td>内网 → 重置 → 公网</td></tr>)}</tbody></table></div>
    <div className="capacity-review-constraints">{study.constraints.map(item => <div key={item.code} className={item.status}><span>{item.status === 'pass' ? <CheckCircle2 size={16}/> : <AlertTriangle size={16}/>}</span><div><strong>{item.label}</strong><small>{item.detail}</small></div></div>)}</div>
    <button type="button" className="button secondary" disabled={pending === 'preflight'} onClick={onPreflight}>{pending === 'preflight' ? <LoaderCircle className="spin" size={15}/> : <RefreshCw size={15}/>}运行真实环境预检</button>
    {preflight.checks?.length ? <div className="capacity-preflight"><h3>预检结果 · {current ? '当前修订' : '配置已变化，需要重新预检'}</h3>{preflight.checks.map((check, index) => <div key={`${check.scope}-${check.targetId}-${index}`} className={check.passed ? 'pass' : 'fail'}>{check.passed ? <CheckCircle2 size={15}/> : <AlertTriangle size={15}/>}<strong>{check.scope === 'sut' ? name(check.targetId) : `${check.network === 'internal' ? '内网' : '公网'}施压机`}</strong><span>{check.detail}</span></div>)}</div> : null}
    {failed.length > 0 && <label className="capacity-partial-confirm"><input type="checkbox" checked={acknowledge} onChange={event => setAcknowledge(event.target.checked)}/><span>我确认移除预检失败的 {failed.map(name).join('、')}，只测试其余服务器；实际执行矩阵将写入审计。</span></label>}
    <button type="button" className="button primary capacity-start" disabled={!current || Boolean(preflight.generatorFailures?.length) || (failed.length > 0 && !acknowledge) || pending === 'start'} onClick={onStart}>{pending === 'start' ? <LoaderCircle className="spin" size={16}/> : <Play size={16}/>}开始容量测试</button>
  </section>;
}

function CapacityRunView({ study, tab, setTab, pending, error, onCancel, onCleanup, onRefresh }: { study: CapacityStudy; tab: 'leadership' | 'engineering' | 'audit'; setTab: (value: 'leadership' | 'engineering' | 'audit') => void; pending: string; error: string; onCancel: () => void; onCleanup: () => void; onRefresh: () => void }) {
  const active = activeStatuses.has(study.status);
  return <div className="page capacity-run-page"><BackLink to="/capacity">返回容量测试</BackLink><PageHeader title={study.name} description={`状态：${study.status} · 容量单位：成功业务迭代/秒`} actions={<div className="page-actions"><button className="button secondary" onClick={onRefresh}><RefreshCw size={15}/>刷新</button>{active && study.status !== 'cancelling' && <button className="button secondary danger" disabled={pending === 'cancel'} onClick={onCancel}><Square size={14}/>取消并清理</button>}{study.status === 'needs-attention' && <button className="button primary" disabled={pending === 'cleanup'} onClick={onCleanup}><RotateCcw size={15}/>重试清理</button>}</div>}/>
    {study.error && <div className="notice warning"><AlertTriangle size={18}/><div><strong>{study.error.code}</strong><p>{study.error.message}</p></div></div>}{error && <div className="inline-alert"><AlertTriangle size={15}/>{error}</div>}
    <section className="panel capacity-progress-panel"><div className="panel-heading"><div><h2>执行阶段</h2><p>构建、内外网测试和清理均保留时间戳。</p></div>{active && <span className="live-run"><LoaderCircle className="spin" size={14}/>运行中</span>}</div><div className="capacity-timeline">{(study.execution.phases || []).map((phase, index) => <div key={`${phase.id}-${index}`} className={phase.status}><span>{phase.status === 'completed' ? <Check size={14}/> : phase.status === 'failed' ? <AlertTriangle size={14}/> : <Circle size={12}/>}</span><div><strong>{phaseLabels[phase.id] || phase.id}</strong><small>{phase.detail || phase.status} · {new Date(phase.at).toLocaleString()}</small></div></div>)}</div>
      {(study.execution.liveMatrix || []).length > 0 ? <div className="capacity-run-matrix">{study.execution.liveMatrix?.map(row => <article key={`${row.experimentId}-${row.targetId}`}><small>{row.network === 'internal' ? '内网' : '公网'} · {row.targetId}</small><strong>{row.currentLoad == null ? '等待负载点' : `${formatCapacity(row.currentLoad)} iter/s`}</strong><span>SLO {row.sloStatus} · {row.pointStatus}</span><span>区间 {formatCapacity(row.confirmedPass)} ～ {formatCapacity(row.confirmedFail)}</span></article>)}</div> : (study.execution.runs || []).length > 0 && <div className="capacity-run-matrix">{study.execution.runs?.map(run => <article key={run.experimentId}><small>{run.network === 'internal' ? '内网' : '公网'}</small><strong>{run.status}</strong><span>施压机 {run.loadGeneratorTargetId}</span></article>)}</div>}
    </section>
    {study.status === 'needs-attention' && <section className="notice warning"><AlertTriangle size={19}/><div><strong>报告尚不可发布</strong><p>至少一个隔离环境没有清理成功。查看审计清单并重试清理。</p></div></section>}
    {study.report && <><nav className="tabs capacity-report-tabs" aria-label="容量报告"><button className={tab === 'leadership' ? 'active' : ''} onClick={() => setTab('leadership')}>领导摘要</button><button className={tab === 'engineering' ? 'active' : ''} onClick={() => setTab('engineering')}>工程证据</button><button className={tab === 'audit' ? 'active' : ''} onClick={() => setTab('audit')}>审计</button></nav>{tab === 'leadership' ? <LeadershipReport study={study}/> : tab === 'engineering' ? <EngineeringReport study={study}/> : <AuditReport study={study}/>}</>}
  </div>;
}

function LeadershipReport({ study }: { study: CapacityStudy }) {
  return <section className="capacity-report"><div className="capacity-report-summary"><Gauge size={24}/><div><small>决策口径 · 置信水平 {Math.round((study.report?.confidenceLevel || 0) * 100)}%</small><strong>最高确认通过负载 ～ 最低确认失败负载</strong><p>{study.report?.decision}</p></div></div>{study.report?.networks.map(network => <section key={network.network} className="panel"><div className="panel-heading"><div><h2>{network.network === 'internal' ? '内网容量' : '公网容量'}</h2><p>{network.status === 'resolved' ? '容量区间已确认' : `证据未闭合：${network.terminationReason || network.status}`}</p></div><span>{network.targets.length} 台</span></div><div className="capacity-result-grid">{network.targets.map(target => { const frontier = firstFrontier(target); return <article key={target.target_id} className="capacity-result-card"><span className={`status ${frontier?.status === 'resolved' ? 'status-completed' : 'status-failed'}`}><span/>{frontier?.status || target.status}</span><h3>{target.label}</h3><div className="capacity-interval"><strong>{formatCapacity(frontier?.confirmed_pass)}</strong><span>≤ 容量 &lt;</span><strong>{formatCapacity(frontier?.confirmed_fail)}</strong></div><p>有效 block {target.valid_block_count} · 无效 {target.invalid_block_count} · 共 {target.attempt_count} 次</p></article>; })}</div></section>)}</section>;
}

function EngineeringReport({ study }: { study: CapacityStudy }) {
  return <section className="capacity-engineering">{study.report?.networks.map(network => <section key={network.network} className="panel table-panel"><div className="panel-heading"><div><h2>{network.network === 'internal' ? '内网' : '公网'}负载轨迹</h2><p>每个点都可追溯到实际 Experiment、原始结果和内容寻址 Artifact。</p></div><Link className="text-link" to={`/experiments/${encodeURIComponent(network.experimentId)}`}>打开原始证据</Link></div><div className="table-wrap"><table><thead><tr><th>序号</th><th>负载</th><th>来源</th><th>状态</th><th>重复数</th><th>原因</th></tr></thead><tbody>{network.trajectory.map((point, index) => <tr key={String(point.load_point_id || index)}><td>{String(point.sequence ?? index + 1)}</td><td>{String(point.offered_load ?? '—')} iter/s</td><td>{String(point.origin || '—')}</td><td>{String(point.status || '—')}</td><td>{String(point.required_repeats || '—')}</td><td>{String(point.reason || '—')}</td></tr>)}</tbody></table></div><div className="capacity-metric-grid">{network.targets.map(target => <article key={target.target_id}><strong>{target.label}</strong>{target.metrics.filter(metric => ['latency_p99_ms','latency_p999_ms','error_ratio','timeout_ratio','success_rate'].includes(metric.metric)).map(metric => <span key={metric.metric}><small>{metric.metric}</small>{metric.raw == null ? '—' : metric.raw.toLocaleString()} {metric.unit}</span>)}</article>)}</div><details className="capacity-evidence"><summary>原始证据计数与摘要</summary><pre>{JSON.stringify(network.evidence, null, 2)}</pre></details></section>)}</section>;
}

function AuditReport({ study }: { study: CapacityStudy }) {
  return <section className="panel capacity-audit-report"><h2>执行与清理审计</h2><dl><div><dt>源码摘要</dt><dd><code>{study.sourceDigest}</code></dd></div><div><dt>选择的被测机</dt><dd>{study.execution.selectedTargetIds?.join('、') || '—'}</dd></div><div><dt>实际被测机</dt><dd>{study.execution.activeTargetIds?.join('、') || '—'}</dd></div><div><dt>明确排除</dt><dd>{study.execution.excludedTargetIds?.join('、') || '无'}</dd></div><div><dt>部分执行确认</dt><dd>{study.execution.acknowledgedPartial ? '是' : '否'}</dd></div><div><dt>最长时间 / 探测次数</dt><dd>{study.execution.budget ? `${study.execution.budget.maxSeconds} 秒 / ${study.execution.budget.maxAttempts} 次` : '—'}</dd></div><div><dt>增量费用控制</dt><dd>{study.execution.costControl ? `${study.execution.costControl.estimatedIncrementalAmount} / ${study.execution.costControl.limit} ${study.execution.costControl.currency} · ${study.execution.costControl.detail}` : '—'}</dd></div></dl><h3>清理证明</h3><div className="capacity-cleanup-list">{study.execution.cleanup?.map(item => <div key={item.targetId} className={item.status}><span>{item.status === 'clean' ? <CheckCircle2 size={16}/> : <AlertTriangle size={16}/>}</span><strong>{item.targetId}</strong><small>{item.cleanedAt ? new Date(item.cleanedAt).toLocaleString() : item.detail}</small></div>) || <p>尚无清理结果。</p>}</div><details className="capacity-evidence"><summary>构建证据</summary>{study.draft.build.evidence.map(item => <code key={`${item.file}:${item.startLine}`}>{item.file}:{item.startLine}-{item.endLine}</code>)}</details></section>;
}
