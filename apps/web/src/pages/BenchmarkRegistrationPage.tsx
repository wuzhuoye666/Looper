import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Bot, CheckCircle2, ChevronLeft, Circle, Copy, FileCode2, Network, Server, ShieldCheck, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { API_BASE, ApiError, api } from '../lib/api';
import type { BenchmarkInputDeclaration, BenchmarkRegistration } from '../lib/types';

const REGISTRATION_ID_KEY = 'looper.benchmark-registration-id.v1';
const AGENT_SKILL_DOWNLOAD = `${API_BASE}/benchmark-skills/looper-benchmark-configure`;
const AGENT_INSTALL_PROMPT = '请安装我附加的 looper-benchmark-configure.zip 为本机 Codex Skill。然后使用 $looper-benchmark-configure 调研并接入这个 Benchmark：<填写套件源码或目录、测试目标和约束>。让套件适配 Looper 的 manifest、Adapter 输入输出、自动 prepare 环境部署和机器拓扑合同；完成本地 schema/fixture 校验后，将 benchmark.yaml、Adapter 脚本及必要资源打成一个 ZIP 接入包。我只把这个包导入 Looper，不手工安装测试依赖。';

type JsonObject = Record<string, unknown>;

function restoreRegistrationId() {
  try { return window.localStorage.getItem(REGISTRATION_ID_KEY) || ''; } catch { return ''; }
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '请求失败';
}

function object(value: unknown): JsonObject {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonObject : {};
}

function array(value: unknown): JsonObject[] {
  return Array.isArray(value) ? value.filter(item => item && typeof item === 'object' && !Array.isArray(item)) as JsonObject[] : [];
}

function manifestInputs(manifest?: JsonObject): BenchmarkInputDeclaration[] {
  const inputs = object(object(object(manifest).spec).adapter).inputs;
  return Array.isArray(inputs) ? inputs as BenchmarkInputDeclaration[] : [];
}

function countLabel(group: JsonObject) {
  const count = object(group.count);
  const minimum = Number(count.minimum || 1);
  const normal = Number(count.default || minimum);
  const maximum = Number(count.maximum || normal);
  return minimum === maximum ? `${minimum} 台` : `${minimum}–${maximum} 台（默认 ${normal}）`;
}

function requirementLabel(group: JsonObject) {
  const requirements = object(group.requirements);
  const cpu = object(requirements.cpu);
  const memory = object(requirements.memory);
  const accelerators = array(requirements.accelerators);
  const parts: string[] = [];
  if (Array.isArray(requirements.architectures) && requirements.architectures.length) parts.push(requirements.architectures.join('/'));
  if (cpu.minimumLogicalCpus) parts.push(`≥ ${cpu.minimumLogicalCpus} 逻辑 CPU`);
  if (memory.minimumGiB) parts.push(`≥ ${memory.minimumGiB} GiB 内存`);
  accelerators.forEach(item => parts.push(`≥ ${item.minimumCount || 1} ${String(item.kind || 'accelerator').toUpperCase()}`));
  const network = object(requirements.network);
  if (network.minimumGbps) parts.push(`≥ ${network.minimumGbps} Gbps`);
  return parts.join(' · ') || '未声明硬件下限';
}

export function BenchmarkRegistrationPage() {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { registrationId = '' } = useParams();
  const resumeId = registrationId || restoreRegistrationId();
  const fileInput = useRef<HTMLInputElement>(null);
  const [promptCopied, setPromptCopied] = useState(false);
  const [configurationName, setConfigurationName] = useState('');
  const [smokeBindings, setSmokeBindings] = useState<Record<string, { reference: string; digest: string }>>({});
  const [record, setRecord] = useState<BenchmarkRegistration>();

  const existing = useQuery({
    queryKey: ['benchmark-registration', resumeId],
    queryFn: () => api.benchmarkRegistration(resumeId),
    enabled: Boolean(resumeId) && record?.id !== resumeId,
    retry: false,
  });

  useEffect(() => {
    if (!existing.data) return;
    setRecord(existing.data);
    if (!registrationId) navigate(`/benchmarks/register/${existing.data.id}`, { replace: true });
  }, [existing.data, navigate, registrationId]);

  useEffect(() => {
    if (!(existing.error instanceof ApiError) || existing.error.status !== 404) return;
    try { window.localStorage.removeItem(REGISTRATION_ID_KEY); } catch { /* Ignore storage failures. */ }
    if (registrationId) navigate('/benchmarks/register', { replace: true });
  }, [existing.error, navigate, registrationId]);

  const acceptRecord = (next: BenchmarkRegistration) => {
    setRecord(next);
    try { window.localStorage.setItem(REGISTRATION_ID_KEY, next.id); } catch { /* Server remains authoritative. */ }
    if (registrationId !== next.id) navigate(`/benchmarks/register/${next.id}`, { replace: true });
  };

  const importMutation = useMutation({
    mutationFn: (configuration: File) => api.importBenchmarkRegistration(configuration),
    onSuccess: acceptRecord,
  });
  const registerMutation = useMutation({
    mutationFn: () => {
      if (!record) throw new Error('请先导入 Benchmark 接入包');
      return api.registerBenchmark(record.id, record.revision);
    },
    onSuccess: next => {
      acceptRecord(next);
      queryClient.invalidateQueries({ queryKey: ['benchmarks'] });
    },
  });

  const draft = record?.draft;
  const manifest = draft?.manifest;
  const smokeInputs = manifestInputs(manifest);
  const smokeBindingsReady = smokeInputs.every(input => !input.required || (Boolean(smokeBindings[input.id]?.reference) && (!input.digestRequired || /^sha256:[0-9a-f]{64}$/.test(smokeBindings[input.id]?.digest || ''))));
  const smokeMutation = useMutation({
    mutationFn: () => {
      if (!draft) throw new Error('Benchmark 尚未登记');
      return api.createBenchmarkSmokeRun(draft.benchmarkId, draft.version, {
        inputBindings: Object.fromEntries(smokeInputs.filter(input => smokeBindings[input.id]?.reference).map(input => [input.id, {
          kind: input.kind,
          reference: smokeBindings[input.id].reference,
          digest: smokeBindings[input.id].digest || undefined,
        }])),
      });
    },
    onSuccess: experiment => navigate(`/experiments/${experiment.id}`),
  });

  const reset = () => {
    try { window.localStorage.removeItem(REGISTRATION_ID_KEY); } catch { /* Ignore storage failures. */ }
    setRecord(undefined); setConfigurationName(''); setSmokeBindings({});
    navigate('/benchmarks/register', { replace: true });
  };
  const updateSmokeBinding = (inputId: string, key: 'reference' | 'digest', value: string) => {
    setSmokeBindings(current => ({ ...current, [inputId]: { ...(current[inputId] || { reference: '', digest: '' }), [key]: value } }));
  };
  const copyAgentPrompt = async () => {
    try { await navigator.clipboard.writeText(AGENT_INSTALL_PROMPT); setPromptCopied(true); } catch { setPromptCopied(false); }
  };

  const locked = record?.status === 'registered';
  const mutationError = importMutation.error || registerMutation.error || smokeMutation.error;
  const returnedConstraints = mutationError instanceof ApiError && mutationError.body && typeof mutationError.body === 'object' && 'constraints' in mutationError.body
    ? (mutationError.body.constraints as BenchmarkRegistration['constraints']) : undefined;
  const constraints = returnedConstraints || record?.constraints || [];
  const blockingConstraints = constraints.filter(item => item.blocking);
  const failedBlocking = blockingConstraints.filter(item => item.status === 'fail');
  const failedAdvisories = constraints.filter(item => !item.blocking && item.status === 'fail');
  const passedBlocking = blockingConstraints.length - failedBlocking.length;
  const orderedConstraints = [...constraints].sort((left, right) => {
    if (left.status !== right.status) return left.status === 'fail' ? -1 : 1;
    if (left.blocking !== right.blocking) return left.blocking ? -1 : 1;
    return left.group.localeCompare(right.group);
  });

  const spec = object(manifest?.spec);
  const metadata = object(manifest?.metadata);
  const scenario = object(spec.scenario);
  const infrastructure = object(spec.infrastructure);
  const audit = object(spec.audit);
  const runtime = object(spec.runtime);
  const nodeGroups = array(infrastructure.nodeGroups);
  const artifacts = array(object(spec.outputs).artifacts);
  const progressSteps = [
    { label: '导入接入包', detail: record ? `${draft?.name} · ${draft?.version}` : 'Benchmark ZIP（含 manifest 与脚本）', done: Boolean(record) },
    { label: '自动检查', detail: record ? failedBlocking.length ? `${failedBlocking.length} 项需回包修改` : '接口门禁通过' : '导入后自动执行', done: Boolean(record) && failedBlocking.length === 0 },
    { label: '进入目录', detail: locked ? record?.benchmarkKey || '版本已锁定' : '一键登记，不等于审计准入', done: locked },
  ];
  const activeProgressStep = locked ? 2 : record ? 1 : 0;
  const progressTitle = locked ? '已进入 Benchmark 目录' : record?.readyToRegister ? '接入包可登记' : record ? `接入包有 ${failedBlocking.length} 个阻断项` : '导入一个完整 Benchmark ZIP';
  const progressDetail = locked
    ? `已锁定 ${record?.benchmarkKey}；正式选型审计仍是后续流程。`
    : record?.readyToRegister
      ? 'Looper 已从配置读取全部注册信息，点击“登记到目录”即可。'
      : failedBlocking[0]
        ? `请在套件接入包中修正：${failedBlocking[0].label}。`
    : '通过 Skill 或接入文档生成 ZIP；Looper 会保存脚本，并在用户选择机器后自动下发。';

  return <div className="page benchmark-registration-page">
    <Link className="back-link" to="/benchmarks"><ChevronLeft size={16}/>返回场景目录</Link>
    <PageHeader title="注册 Benchmark" description="注册页只导入标准接入包；身份、机器拓扑、执行方式、指标和证据均以 manifest 为唯一事实源。" actions={<>
      {record&&<button className="button secondary" onClick={reset}>{locked ? '注册另一个' : '更换配置'}</button>}
      {record?.readyToRegister&&!locked&&<button className="button primary" disabled={registerMutation.isPending} onClick={()=>registerMutation.mutate()}><ShieldCheck size={16}/>{registerMutation.isPending ? '正在登记…' : '登记到目录'}</button>}
      {locked&&draft?.executionStatus==='executable'&&<button className="button primary" disabled={smokeMutation.isPending||!smokeBindingsReady} onClick={()=>smokeMutation.mutate()}><ShieldCheck size={16}/>创建冒烟实验</button>}
    </>}/>

    <section className={`registration-progress panel ${locked?'complete':record?.readyToRegister?'ready':record?'blocked':'idle'}`} aria-label="注册进度">
      <div className="registration-progress-summary"><small>当前状态</small><h2>{progressTitle}</h2><p>{progressDetail}</p></div>
      <ol>{progressSteps.map((item,index)=><li className={item.done?'done':index===activeProgressStep?'active':''} key={item.label}><span>{item.done?<CheckCircle2 size={15}/>:index+1}</span><div><strong>{item.label}</strong><small>{item.detail}</small></div></li>)}</ol>
    </section>

    {!record&&<div className="benchmark-import panel"><div className="benchmark-import-summary"><Upload size={19}/><span><strong>从标准接入包开始</strong><small>推荐 ZIP（含 benchmark.yaml 与 Adapter 脚本）；Stage 0 合同仍可只导入 YAML/JSON。上传只校验和保存，不会在控制平面执行套件代码。</small></span></div><div className="benchmark-import-tools"><div><a className="button agent-skill-button compact" href={AGENT_SKILL_DOWNLOAD} download="looper-benchmark-configure.zip"><Bot size={14}/>下载接入 Skill</a><button type="button" className="button secondary compact" onClick={copyAgentPrompt}><Copy size={14}/>{promptCopied?'已复制':'复制使用提示词'}</button><button type="button" className="button primary" disabled={importMutation.isPending} onClick={()=>fileInput.current?.click()}><Upload size={15}/>{importMutation.isPending?'正在解析和检查…':'选择 Benchmark ZIP'}</button><input ref={fileInput} className="benchmark-file-input" aria-label="Benchmark 接入包" type="file" accept=".zip,application/zip,.yaml,.yml,.json,application/json,text/yaml" disabled={importMutation.isPending} onChange={event=>{const file=event.target.files?.[0];if(file){setConfigurationName(file.name);importMutation.mutate(file);}event.target.value='';}}/></div><small className="codex-registration-hint">{configurationName?`正在处理 ${configurationName}`:'开发者交付接入包；Looper 在实验启动时自动部署。'}</small></div><details className="agent-prompt"><summary>查看给 Codex 的使用提示词</summary><textarea aria-label="给本机 Codex 的安装提示词" readOnly rows={5} value={AGENT_INSTALL_PROMPT}/></details></div>}

    {existing.isError&&<div className="notice error"><AlertTriangle size={18}/><div><strong>服务端记录无法恢复</strong><p>{errorMessage(existing.error)}。请清空本地指针后重新导入。</p></div></div>}
    {mutationError&&<div className="notice error"><AlertTriangle size={18}/><div><strong>操作未完成</strong><p>{errorMessage(mutationError)}</p></div></div>}

    {record&&<div className="registration-layout">
      <div className="registration-form-stack">
        <section className="panel registration-section"><div className="panel-heading"><div><h2>自动识别的接入合同</h2><p>以下内容均直接来自 manifest，页面不可修改。</p></div><FileCode2 size={19}/></div><div className="registration-package-summary">
          <article><small>身份与来源</small><strong>{draft?.name}</strong><p>{draft?.benchmarkId}@{draft?.version}</p><p>{String(metadata.license || '未声明许可证')} · {draft?.sourceRevision ? `固定 ${draft.sourceRevision.slice(0, 12)}…` : '来源未固定'}</p></article>
          <article><small>执行接口</small><strong>{draft?.executionModel || 'custom'}</strong><p>{String(runtime.type || '未知运行时')} · {draft?.executionStatus}</p><p>{record.packageReady?'完整脚本包已保存':'仅 manifest'} · {manifestInputs(manifest).length} 个命名输入 · {artifacts.length} 个证据产物</p></article>
          <article><small>场景语义</small><strong>{String(scenario.topology || '未声明 topology')}</strong><p>{draft?.primaryMetric || '未声明主指标'}{draft?.primaryUnit ? ` · ${draft.primaryUnit}` : ''}</p><p>{String(scenario.decision_question || '非选型型套件；未声明采购问题')}</p></article>
          <article><small>审计默认值</small><strong>{audit.minimumRepeats ? `${audit.minimumRepeats} 次重复` : `${draft?.repeats || 3} 次兼容默认`}</strong><p>Reference：{String(audit.referencePolicy || '未声明')}</p><p>环境轴：{Array.isArray(audit.environmentAxes)&&audit.environmentAxes.length ? audit.environmentAxes.join(' / ') : '未声明'}</p></article>
        </div></section>

        <section className="panel registration-section"><div className="panel-heading"><div><h2>机器与拓扑</h2><p>多机套件由机器组定义数量范围、最低机型、放置关系和网络连接。</p></div><Network size={19}/></div>{nodeGroups.length?<div className="registration-node-groups">{nodeGroups.map(group=><article key={String(group.id)}><Server size={17}/><div><strong>{String(group.id)} · {String(group.role)}</strong><p>{countLabel(group)} · {requirementLabel(group)}</p></div></article>)}</div>:<div className="registration-empty-contract"><AlertTriangle size={17}/><span>未声明 spec.infrastructure；单机套件允许省略，多机/分布式套件会收到提醒。</span></div>}</section>

        {locked&&draft?.executionStatus==='executable'&&smokeInputs.length>0&&<section className="panel registration-section"><div className="panel-heading"><div><h2>冒烟运行输入</h2><p>只绑定资源引用；secret 明文不会进入运行信封。</p></div></div><div className="form-grid registration-fields">{smokeInputs.map(input=><div className="full input-binding-field" key={input.id}><label><span>{input.id} · {input.kind}{input.required?' *':''}</span><input type={input.kind==='secret'?'password':'text'} value={smokeBindings[input.id]?.reference||''} onChange={event=>updateSmokeBinding(input.id,'reference',event.target.value)} placeholder={input.kind==='secret'?'secret://受管密钥名称':'资源引用 URI / 目标设备引用'}/><small>{input.description||'运行前绑定的协议输入。'}</small></label>{input.digestRequired&&<label><span>SHA-256 digest *</span><input value={smokeBindings[input.id]?.digest||''} onChange={event=>updateSmokeBinding(input.id,'digest',event.target.value)} placeholder="sha256:…"/></label>}</div>)}</div></section>}

        <details className="panel registration-manifest-details"><summary>查看完整 manifest</summary><pre>{JSON.stringify(manifest, null, 2)}</pre></details>
        <div className="registration-actions">{!locked&&<button type="button" className="button secondary" onClick={reset}>修改接入包后重新导入</button>}{locked&&<><span className="draft-saved"><CheckCircle2 size={15}/>已登记 {record.benchmarkKey}</span><Link className="button primary" to="/benchmarks">查看场景目录</Link></>}</div>
      </div>

      <aside className="constraint-panel panel" aria-label="Benchmark 注册约束"><div className="constraint-heading"><ShieldCheck size={20}/><div><h2>{locked?'登记时门禁快照':'自动接口检查'}</h2><p>{constraints.length?`${passedBlocking} / ${blockingConstraints.length} 个阻断项通过${failedAdvisories.length?` · ${failedAdvisories.length} 个后续提醒`:''}`:'等待配置'}</p></div></div><div className="constraint-progress" aria-label={`阻断项通过 ${passedBlocking} / ${blockingConstraints.length}`}><span style={{width:`${blockingConstraints.length?passedBlocking/blockingConstraints.length*100:0}%`}}/></div>{failedBlocking[0]&&<div className="constraint-next"><strong>请回接入包修改</strong><p>{failedBlocking[0].label}</p></div>}<ol>{orderedConstraints.map(item=><li className={`${item.status==='pass'?'passed':'failed'}${item.blocking?'':' advisory'}`} key={item.code}>{item.status==='pass'?<CheckCircle2 size={16}/>:<Circle size={16}/>}<div><small>{item.group} · {item.blocking?'注册阻断':'准入提醒'} · {item.code}</small><span>{item.label}</span><p>{item.detail}</p></div></li>)}</ol><div className="constraint-footnote"><strong>两条线分开</strong><p>注册阻断只判断接入包是否安全、完整、可解释；跨机器审计和 Reference 证据属于后续正式准入，不再要求注册时手工勾选。</p></div></aside>
    </div>}
  </div>;
}
