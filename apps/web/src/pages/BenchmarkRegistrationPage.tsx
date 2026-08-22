import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, ChevronLeft, Circle, FileCode2, Save, ShieldCheck } from 'lucide-react';
import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { ApiError, api } from '../lib/api';
import type { BenchmarkRegistration, BenchmarkRegistrationDraft } from '../lib/types';

const REGISTRATION_ID_KEY = 'looper.benchmark-registration-id.v1';

const emptyDraft: BenchmarkRegistrationDraft = {
  name: '', benchmarkId: '', version: '0.1.0', sourceUrl: '', sourceRevision: '', license: '',
  category: 'cpu-iaas', decisionQuestion: '', primaryMetric: '', primaryUnit: '', correctnessContract: '',
  runtimeType: 'container', executionStatus: 'stage0-adapter-only', image: '', minimumSamples: 1,
  repeats: 3, hasReference: false, retainsRawEvidence: true, crossEnvironmentAudit: true,
};

function restoreRegistrationId() {
  try { return window.localStorage.getItem(REGISTRATION_ID_KEY) || ''; } catch { return ''; }
}

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : '请求失败';
}

export function BenchmarkRegistrationPage() {
  const queryClient = useQueryClient();
  const [resumeId] = useState(restoreRegistrationId);
  const [draft, setDraft] = useState<BenchmarkRegistrationDraft>(emptyDraft);
  const [manifestText, setManifestText] = useState('');
  const [parseError, setParseError] = useState('');
  const [record, setRecord] = useState<BenchmarkRegistration>();
  const existing = useQuery({
    queryKey: ['benchmark-registration', resumeId],
    queryFn: () => api.benchmarkRegistration(resumeId),
    enabled: Boolean(resumeId),
    retry: false,
  });

  useEffect(() => {
    if (!existing.data) return;
    setRecord(existing.data);
    setDraft(existing.data.draft);
    setManifestText(existing.data.draft.manifest ? JSON.stringify(existing.data.draft.manifest, null, 2) : '');
  }, [existing.data]);

  const acceptRecord = (next: BenchmarkRegistration) => {
    setRecord(next);
    try { window.localStorage.setItem(REGISTRATION_ID_KEY, next.id); } catch { /* Server remains authoritative. */ }
  };
  const update = <K extends keyof BenchmarkRegistrationDraft>(key: K, value: BenchmarkRegistrationDraft[K]) => {
    setDraft(current => ({ ...current, [key]: value }));
  };
  const payload = () => {
    setParseError('');
    if (!manifestText.trim()) return { ...draft, manifest: undefined };
    try {
      const manifest = JSON.parse(manifestText) as unknown;
      if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) throw new Error();
      return { ...draft, manifest: manifest as Record<string, unknown> };
    } catch {
      setParseError('manifest 必须是合法的 JSON 对象。YAML 请先转换为 JSON。');
      return undefined;
    }
  };
  const saveMutation = useMutation({
    mutationFn: async () => {
      const next = payload();
      if (!next) throw new Error('manifest JSON 解析失败');
      return record
        ? api.updateBenchmarkRegistration(record.id, record.revision, next)
        : api.createBenchmarkRegistration(next);
    },
    onSuccess: acceptRecord,
  });
  const registerMutation = useMutation({
    mutationFn: () => {
      if (!record) throw new Error('请先保存草稿');
      return api.registerBenchmark(record.id, record.revision);
    },
    onSuccess: next => {
      acceptRecord(next);
      queryClient.invalidateQueries({ queryKey: ['benchmarks'] });
    },
  });
  const reset = () => {
    try { window.localStorage.removeItem(REGISTRATION_ID_KEY); } catch { /* Ignore storage failures. */ }
    setRecord(undefined); setDraft(emptyDraft); setManifestText(''); setParseError('');
  };
  const locked = record?.status === 'registered';
  const constraints = record?.constraints || [];
  const mutationError = saveMutation.error || registerMutation.error;
  const failedConstraints = mutationError instanceof ApiError && mutationError.body && typeof mutationError.body === 'object' && 'constraints' in mutationError.body
    ? (mutationError.body.constraints as BenchmarkRegistration['constraints']) : undefined;
  const visibleConstraints = failedConstraints || constraints;
  const passed = visibleConstraints.filter(item => item.status === 'pass').length;

  return <div className="page benchmark-registration-page">
    <Link className="back-link" to="/benchmarks"><ChevronLeft size={16}/>返回场景目录</Link>
    <PageHeader title="注册 Benchmark" description="服务端保存可追溯合同并计算约束；登记成功仍不等于安装、信任或审计准入。" actions={<><button className="button secondary" onClick={reset}>清空 / 新建</button><button className="button primary" disabled={locked || saveMutation.isPending} onClick={()=>saveMutation.mutate()}><Save size={16}/>{record?'更新服务端草稿':'保存服务端草稿'}</button></>}/>
    <div className="notice warning"><AlertTriangle size={18}/><div><strong>当前实现边界</strong><p>注册 API 只允许完整 manifest 通过校验的 Stage 0 合同进入目录，并强制标记为未信任、不可执行。可执行 bundle 的安装、签名验证和人工信任审批仍是独立门禁。</p></div></div>
    {existing.isError&&<div className="notice error"><AlertTriangle size={18}/><div><strong>服务端草稿无法恢复</strong><p>{errorMessage(existing.error)}。可清空当前指针后新建。</p></div></div>}
    {(parseError||mutationError)&&<div className="notice error"><AlertTriangle size={18}/><div><strong>操作未完成</strong><p>{parseError||errorMessage(mutationError)}</p></div></div>}
    <fieldset disabled={locked} className="registration-fieldset">
      <div className="registration-layout">
        <div className="registration-form-stack">
          <section className="panel registration-section"><div className="panel-heading"><div><h2>1. 身份与来源</h2><p>版本和来源进入审计后保持不可变。</p></div><FileCode2 size={19}/></div><div className="form-grid registration-fields">
            <label><span>Benchmark 名称 *</span><input value={draft.name} onChange={e=>update('name',e.target.value)} placeholder="例如 TC Web Service"/></label>
            <label><span>Benchmark ID *</span><input value={draft.benchmarkId} onChange={e=>update('benchmarkId',e.target.value)} placeholder="tc.web-service"/><small>服务端按 v1alpha1 schema 校验。</small></label>
            <label><span>版本 *</span><input value={draft.version} onChange={e=>update('version',e.target.value)}/></label>
            <label><span>许可证 *</span><input value={draft.license} onChange={e=>update('license',e.target.value)} placeholder="SPDX identifier 或内部许可编号"/></label>
            <label className="full"><span>源码地址 *</span><input value={draft.sourceUrl} onChange={e=>update('sourceUrl',e.target.value)} placeholder="https://..."/></label>
            <label className="full"><span>固定 revision *</span><input value={draft.sourceRevision} onChange={e=>update('sourceRevision',e.target.value)} placeholder="完整 commit SHA 或 sha256 digest"/></label>
          </div></section>
          <section className="panel registration-section"><div className="panel-heading"><div><h2>2. 场景与结果契约</h2><p>页面摘要必须与 manifest 完全一致，避免双重事实源。</p></div></div><div className="form-grid registration-fields">
            <label><span>场景类型 *</span><select value={draft.category} onChange={e=>update('category',e.target.value)}><option value="cpu-iaas">CPU / 综合 IaaS</option><option value="online-service">在线服务</option><option value="database">数据库</option><option value="storage">存储</option><option value="gpu-ai">GPU / AI</option><option value="network">网络</option></select></label>
            <label><span>主指标 *</span><input value={draft.primaryMetric} onChange={e=>update('primaryMetric',e.target.value)} placeholder="例如 slo_goodput"/></label>
            <label className="full"><span>采购决策问题 *</span><textarea rows={3} value={draft.decisionQuestion} onChange={e=>update('decisionQuestion',e.target.value)} placeholder="这个 Benchmark 要支持什么选型决策？"/></label>
            <label><span>主指标单位 *</span><input value={draft.primaryUnit} onChange={e=>update('primaryUnit',e.target.value)} placeholder="例如 requests/s"/></label>
            <label><span>minimumSamples *</span><input type="number" min={1} value={draft.minimumSamples} onChange={e=>update('minimumSamples',Number(e.target.value))}/></label>
            <label className="full"><span>正确性 / 业务质量门禁 *</span><textarea rows={3} value={draft.correctnessContract} onChange={e=>update('correctnessContract',e.target.value)} placeholder="描述不可补偿的正确性、SLO、RPO/RTO 或质量约束。"/></label>
          </div></section>
          <section className="panel registration-section"><div className="panel-heading"><div><h2>3. 执行、证据与审计</h2><p>注册只接受 Stage 0；执行安装走后续信任链。</p></div></div><div className="form-grid registration-fields">
            <label><span>运行时 *</span><select value={draft.runtimeType} onChange={e=>update('runtimeType',e.target.value as BenchmarkRegistrationDraft['runtimeType'])}><option value="container">容器</option><option value="local-process">本地进程</option><option value="benchexec">BenchExec</option></select></label>
            <label><span>执行成熟度 *</span><select value={draft.executionStatus} onChange={e=>update('executionStatus',e.target.value as BenchmarkRegistrationDraft['executionStatus'])}><option value="stage0-adapter-only">Stage 0 · 仅适配器</option><option value="executable">Executable · 需独立安装流程</option></select></label>
            <label className="full"><span>容器镜像 digest</span><input value={draft.image} onChange={e=>update('image',e.target.value)} placeholder="registry/image@sha256:..."/><small>值必须与 manifest runtime.image 一致。</small></label>
            <label><span>每个环境重复次数 *</span><input type="number" min={1} max={1000} value={draft.repeats} onChange={e=>update('repeats',Number(e.target.value))}/></label>
            <div className="registration-check-fields"><label><input type="checkbox" checked={draft.hasReference} onChange={e=>update('hasReference',e.target.checked)}/><span>提供 Base 与 Reference</span></label><label><input type="checkbox" checked={draft.retainsRawEvidence} onChange={e=>update('retainsRawEvidence',e.target.checked)}/><span>保留原始证据</span></label><label><input type="checkbox" checked={draft.crossEnvironmentAudit} onChange={e=>update('crossEnvironmentAudit',e.target.checked)}/><span>进入跨环境审计</span></label></div>
          </div></section>
          <section className="panel registration-section"><div className="panel-heading"><div><h2>4. 完整 manifest</h2><p>这是准入计算的规范事实源；当前输入格式为 JSON。</p></div></div><label className="manifest-editor"><span>Benchmark manifest JSON *</span><textarea rows={18} value={manifestText} onChange={e=>setManifestText(e.target.value)} spellCheck={false} placeholder={'{\n  "apiVersion": "looper.dev/v1alpha1",\n  ...\n}'}/></label></section>
          <div className="registration-actions"><button type="button" className="button secondary" onClick={reset}>清空 / 新建</button><button type="button" className="button primary" disabled={locked||saveMutation.isPending} onClick={()=>saveMutation.mutate()}><Save size={16}/>保存并重新校验</button><button type="button" className="button primary" disabled={!record?.readyToRegister||locked||registerMutation.isPending} onClick={()=>registerMutation.mutate()}><ShieldCheck size={16}/>登记到目录（不可执行）</button>{record&&<span className="draft-saved"><CheckCircle2 size={15}/>{locked?`已登记 ${record.benchmarkKey}`:`服务端草稿 r${record.revision}`}</span>}</div>
        </div>
        <aside className="constraint-panel panel" aria-label="Benchmark 注册约束"><div className="constraint-heading"><ShieldCheck size={20}/><div><h2>服务端约束</h2><p>{visibleConstraints.length?`${passed} / ${visibleConstraints.length} 项通过`:'保存后由服务端计算'}</p></div></div><div className="constraint-progress"><span style={{width:`${visibleConstraints.length?passed/visibleConstraints.length*100:0}%`}}/></div><ol>{visibleConstraints.map(item=><li className={item.status==='pass'?'passed':''} key={item.code}>{item.status==='pass'?<CheckCircle2 size={16}/>:<Circle size={16}/>}<div><small>{item.group} · {item.code}{item.blocking?'':' · 非阻断'}</small><span>{item.label}</span><p>{item.detail}</p></div></li>)}</ol><div className="constraint-footnote"><strong>兼容原则</strong><p>行为字段继续遵循 <code>looper.dev/v1alpha1</code>。旧记录没有注册证据时显示“历史未审计”，不进行可信性推断。</p></div></aside>
      </div>
    </fieldset>
  </div>;
}
