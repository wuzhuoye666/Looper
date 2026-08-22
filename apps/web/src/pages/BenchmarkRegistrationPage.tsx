import { AlertTriangle, CheckCircle2, ChevronLeft, Circle, FileCode2, Save, ShieldCheck } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';

const DRAFT_KEY = 'looper.benchmark-registration-draft.v1';

type RuntimeType = 'container' | 'trusted-local' | 'remote';
type ExecutionStatus = 'stage0-adapter-only' | 'executable';

interface RegistrationDraft {
  name: string;
  benchmarkId: string;
  version: string;
  sourceUrl: string;
  sourceRevision: string;
  license: string;
  category: string;
  decisionQuestion: string;
  primaryMetric: string;
  primaryUnit: string;
  correctnessContract: string;
  runtimeType: RuntimeType;
  executionStatus: ExecutionStatus;
  image: string;
  minimumSamples: number;
  repeats: number;
  hasReference: boolean;
  retainsRawEvidence: boolean;
  crossEnvironmentAudit: boolean;
}

const emptyDraft: RegistrationDraft = {
  name: '', benchmarkId: '', version: '0.1.0', sourceUrl: '', sourceRevision: '', license: '',
  category: 'cpu-iaas', decisionQuestion: '', primaryMetric: '', primaryUnit: '', correctnessContract: '',
  runtimeType: 'container', executionStatus: 'stage0-adapter-only', image: '', minimumSamples: 1,
  repeats: 3, hasReference: false, retainsRawEvidence: true, crossEnvironmentAudit: true,
};

function restoreDraft(): RegistrationDraft {
  try {
    const raw = window.localStorage.getItem(DRAFT_KEY);
    return raw ? { ...emptyDraft, ...JSON.parse(raw) } : emptyDraft;
  } catch {
    return emptyDraft;
  }
}

export function BenchmarkRegistrationPage() {
  const [draft, setDraft] = useState<RegistrationDraft>(restoreDraft);
  const [saved, setSaved] = useState(false);
  const update = <K extends keyof RegistrationDraft>(key: K, value: RegistrationDraft[K]) => {
    setDraft(current => ({ ...current, [key]: value })); setSaved(false);
  };
  const checks = useMemo(() => [
    { group: '身份', label: 'Benchmark ID 使用稳定的小写标识', ok: /^[a-z0-9]+(?:[._-][a-z0-9]+)*$/.test(draft.benchmarkId) },
    { group: '身份', label: '版本与不可变源码 revision 已填写', ok: Boolean(draft.version.trim() && draft.sourceRevision.trim()) },
    { group: '契约', label: '采购问题、主指标和单位明确', ok: Boolean(draft.decisionQuestion.trim() && draft.primaryMetric.trim() && draft.primaryUnit.trim()) },
    { group: '契约', label: '正确性或业务质量门禁明确', ok: Boolean(draft.correctnessContract.trim()) },
    { group: '执行', label: draft.runtimeType === 'container' ? '容器镜像固定到 @sha256 digest' : '非容器运行时需要后端能力和信任审批', ok: draft.runtimeType === 'container' ? /@sha256:[a-f0-9]{64}$/i.test(draft.image) : false },
    { group: '证据', label: '声明 minimumSamples，重复运行不少于当前合同默认值 3', ok: draft.minimumSamples >= 1 && draft.repeats >= 3 },
    { group: '证据', label: '保留原始日志、直方图或 trace 证据', ok: draft.retainsRawEvidence },
    { group: '审计', label: '声明 Base/Reference 与跨环境审计', ok: draft.hasReference && draft.crossEnvironmentAudit },
  ], [draft]);
  const passed = checks.filter(item => item.ok).length;
  const save = () => {
    try { window.localStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); setSaved(true); }
    catch { setSaved(false); }
  };
  const reset = () => { window.localStorage.removeItem(DRAFT_KEY); setDraft(emptyDraft); setSaved(false); };

  return <div className="page benchmark-registration-page">
    <Link className="back-link" to="/benchmarks"><ChevronLeft size={16}/>返回场景目录</Link>
    <PageHeader title="注册 Benchmark" description="先建立可审查的场景草稿；当前不会写入控制平面，也不会自动取得准入资格。" actions={<button className="button primary" onClick={save}><Save size={16}/>保存本地草稿</button>}/>
    <div className="notice warning"><AlertTriangle size={18}/><div><strong>当前实现边界</strong><p>草稿仅保存在当前浏览器。服务端注册、manifest 校验、安装审批和准入审计接口接入前，页面不会把草稿加入正式场景目录。</p></div></div>
    <div className="registration-layout">
      <div className="registration-form-stack">
        <section className="panel registration-section"><div className="panel-heading"><div><h2>1. 身份与来源</h2><p>版本和来源一旦进入审计必须保持不可变。</p></div><FileCode2 size={19}/></div><div className="form-grid registration-fields">
          <label><span>Benchmark 名称 *</span><input value={draft.name} onChange={e=>update('name',e.target.value)} placeholder="例如 TC Web Service"/></label>
          <label><span>Benchmark ID *</span><input value={draft.benchmarkId} onChange={e=>update('benchmarkId',e.target.value)} placeholder="tc.web-service"/><small>仅小写字母、数字、点、下划线和连字符。</small></label>
          <label><span>版本 *</span><input value={draft.version} onChange={e=>update('version',e.target.value)}/></label>
          <label><span>许可证 *</span><input value={draft.license} onChange={e=>update('license',e.target.value)} placeholder="SPDX identifier 或内部许可编号"/></label>
          <label className="full"><span>源码地址 *</span><input value={draft.sourceUrl} onChange={e=>update('sourceUrl',e.target.value)} placeholder="https://..."/></label>
          <label className="full"><span>固定 revision *</span><input value={draft.sourceRevision} onChange={e=>update('sourceRevision',e.target.value)} placeholder="commit SHA 或不可变 artifact digest"/></label>
        </div></section>
        <section className="panel registration-section"><div className="panel-heading"><div><h2>2. 场景与结果契约</h2><p>先写清采购问题，再定义指标；总分不能补偿正确性或 SLO 失败。</p></div></div><div className="form-grid registration-fields">
          <label><span>场景类型 *</span><select value={draft.category} onChange={e=>update('category',e.target.value)}><option value="cpu-iaas">CPU / 综合 IaaS</option><option value="online-service">在线服务</option><option value="database">数据库</option><option value="storage">存储</option><option value="gpu-ai">GPU / AI</option><option value="network">网络</option></select></label>
          <label><span>主指标 *</span><input value={draft.primaryMetric} onChange={e=>update('primaryMetric',e.target.value)} placeholder="例如 slo_goodput"/></label>
          <label className="full"><span>采购决策问题 *</span><textarea rows={3} value={draft.decisionQuestion} onChange={e=>update('decisionQuestion',e.target.value)} placeholder="这个 Benchmark 要支持什么选型决策？"/></label>
          <label><span>主指标单位 *</span><input value={draft.primaryUnit} onChange={e=>update('primaryUnit',e.target.value)} placeholder="例如 requests/s"/></label>
          <label><span>minimumSamples *</span><input type="number" min={1} value={draft.minimumSamples} onChange={e=>update('minimumSamples',Number(e.target.value))}/></label>
          <label className="full"><span>正确性 / 业务质量门禁 *</span><textarea rows={3} value={draft.correctnessContract} onChange={e=>update('correctnessContract',e.target.value)} placeholder="描述不可补偿的正确性、SLO、RPO/RTO 或质量约束。"/></label>
        </div></section>
        <section className="panel registration-section"><div className="panel-heading"><div><h2>3. 执行、证据与审计</h2><p>适配器草稿和真正可执行的 Benchmark 必须显式区分。</p></div></div><div className="form-grid registration-fields">
          <label><span>运行时 *</span><select value={draft.runtimeType} onChange={e=>update('runtimeType',e.target.value as RuntimeType)}><option value="container">容器</option><option value="trusted-local">本地进程（需信任审批）</option><option value="remote">远程执行器（待接入）</option></select></label>
          <label><span>执行成熟度 *</span><select value={draft.executionStatus} onChange={e=>update('executionStatus',e.target.value as ExecutionStatus)}><option value="stage0-adapter-only">Stage 0 · 仅适配器</option><option value="executable">Executable · 可执行</option></select></label>
          <label className="full"><span>容器镜像 digest</span><input value={draft.image} onChange={e=>update('image',e.target.value)} placeholder="registry/image@sha256:..."/><small>可执行容器必须固定 digest，不能使用 latest 或可变 tag。</small></label>
          <label><span>每个环境重复次数 *</span><input type="number" min={1} max={1000} value={draft.repeats} onChange={e=>update('repeats',Number(e.target.value))}/></label>
          <div className="registration-check-fields"><label><input type="checkbox" checked={draft.hasReference} onChange={e=>update('hasReference',e.target.checked)}/><span>提供 Base 与 Reference</span></label><label><input type="checkbox" checked={draft.retainsRawEvidence} onChange={e=>update('retainsRawEvidence',e.target.checked)}/><span>保留原始证据</span></label><label><input type="checkbox" checked={draft.crossEnvironmentAudit} onChange={e=>update('crossEnvironmentAudit',e.target.checked)}/><span>进入跨环境审计</span></label></div>
        </div></section>
        <div className="registration-actions"><button className="button secondary" onClick={reset}>清空草稿</button><button className="button primary" onClick={save}><Save size={16}/>保存本地草稿</button>{saved&&<span className="draft-saved"><CheckCircle2 size={15}/>草稿已保存，尚未注册</span>}</div>
      </div>
      <aside className="constraint-panel panel" aria-label="Benchmark 注册约束"><div className="constraint-heading"><ShieldCheck size={20}/><div><h2>开发约束</h2><p>{passed} / {checks.length} 项已满足</p></div></div><div className="constraint-progress"><span style={{width:`${passed/checks.length*100}%`}}/></div><ol>{checks.map(item=><li className={item.ok?'passed':''} key={`${item.group}-${item.label}`}>{item.ok?<CheckCircle2 size={16}/>:<Circle size={16}/>}<div><small>{item.group}</small><span>{item.label}</span></div></li>)}</ol><div className="constraint-footnote"><strong>兼容原则</strong><p>新增审计字段保持可选；行为字段继续遵循 <code>looper.dev/v1alpha1</code>，扩展信息进入 <code>x-extensions</code>。旧记录缺字段时显示未知，不进行推断。</p></div></aside>
    </div>
  </div>;
}
