import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Activity, AlertTriangle, CheckCircle2, FileCheck2, LoaderCircle, RotateCcw, ShieldCheck, WandSparkles } from 'lucide-react';
import { Link } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';
import type { SystemOptimizationStudy } from '../lib/types';

const STATUS_LABEL: Record<string, string> = {
  draft: '草稿',
  'hypothesis-ready': '假设已生成',
  'awaiting-approval': '待批准',
  applying: '应用配置中',
  measuring: '复测中',
  'rolling-back': '回滚中',
  evaluating: '评估中',
  completed: '已完成',
  'needs-attention': '需要关注',
};

const STATUS_CLASS: Record<string, string> = {
  'hypothesis-ready': 'status-queued',
  'awaiting-approval': 'status-paused',
  applying: 'status-running',
  measuring: 'status-running',
  'rolling-back': 'status-running',
  evaluating: 'status-running',
  completed: 'status-completed',
  'needs-attention': 'status-failed',
};

function sleep(ms: number) { return new Promise(resolve => window.setTimeout(resolve, ms)); }

function StudyStatusBadge({ status }: { status: string }) {
  return <span className={`status ${STATUS_CLASS[status] || 'status-paused'}`}><span />{STATUS_LABEL[status] || status}</span>;
}

function StudyDetail({ study, message, onAction, actionPending }: {
  study: SystemOptimizationStudy;
  message: string;
  onAction: (kind: 'approve' | 'activate' | 'rollback') => void;
  actionPending: string | null;
}) {
  const canApprove = study.status === 'awaiting-approval';
  const canRollback = ['applying', 'measuring', 'evaluating'].includes(study.status)
    || (study.status === 'needs-attention' && (study.orchestration?.rollback as { phase?: string } | undefined)?.phase !== 'verified');
  const phaseLabels: Record<string, string> = { pending: '等待', running: '进行中', verified: '已核实', failed: '失败', completed: '完成' };
  const phases = Object.entries(study.orchestration || {})
    .filter(([key]) => key !== 'schema_version')
    .map(([key, value]) => {
      const phase = value && typeof value === 'object' && 'phase' in value
        ? String((value as Record<string, unknown>).phase ?? '')
        : 'recorded';
      return { key, phase };
    });

  return <div className="system-optimization-study">
    <div style={{ padding: 18 }}>
      <dl className="key-value-list">
        <div><dt>研究 ID</dt><dd><code>{study.id}</code></dd></div>
        <div><dt>状态</dt><dd><StudyStatusBadge status={study.status} /></dd></div>
        <div><dt>目标</dt><dd><code>{study.targetId}</code></dd></div>
        <div><dt>最小效果</dt><dd>{study.minimumEffect}</dd></div>
        <div><dt>授权 profile</dt><dd><code>{study.authorizationProfileDigest}</code></dd></div>
        <div><dt>Hypothesis</dt><dd><code>{study.hypothesisDigest || '尚未生成'}</code></dd></div>
        <div><dt>决策</dt><dd><code>{study.decisionDigest || '尚未裁决'}</code></dd></div>
      </dl>
      {study.problem && <div className="inline-alert" style={{ marginTop: 14 }}>
        <AlertTriangle size={15} /><span>问题：{study.problem.message}{study.problem.suggestedAction ? `（建议：${study.problem.suggestedAction}）` : ''}</span>
      </div>}
      {study.artifacts.length > 0 && <div style={{ marginTop: 16 }}>
        <p className="field-label">证据工件（{study.artifacts.length}）</p>
        <ul className="system-optimization-artifacts">
          {study.artifacts.map(artifact => <li key={`${artifact.digest}-${artifact.role}-${artifact.name}`}>
            <FileCheck2 size={13} /><span><strong>{artifact.role}</strong> {artifact.name}</span><code>{artifact.digest}</code>
          </li>)}
        </ul>
      </div>}
      {phases.length > 0 && <div style={{ marginTop: 16 }}>
        <p className="field-label">编排进度</p>
        <div className="system-optimization-phases">
          {phases.map(item => <span key={item.key}><i>{item.key}</i><b className={item.phase === 'verified' || item.phase === 'completed' ? 'phase-ok' : item.phase === 'failed' ? 'phase-bad' : ''}>{phaseLabels[item.phase] || item.phase}</b></span>)}
        </div>
      </div>}
      <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
        {canApprove && <button className="button primary" disabled={!!actionPending} onClick={() => onAction('approve')}>
          <ShieldCheck size={15} />{actionPending === 'approve' ? '正在批准…' : '批准并开始应用'}
        </button>}
        {canRollback && <button className="button danger" disabled={!!actionPending} onClick={() => onAction('rollback')}>
          <RotateCcw size={15} />{actionPending === 'rollback' ? '正在回滚…' : '回滚配置'}
        </button>}
        {!canApprove && !canRollback && study.status !== 'completed' && study.status !== 'needs-attention' &&
          <span className="system-optimization-note">平台正在执行编排流程，页面自动跟随状态变化；无需操作。</span>}
      </div>
      <div className="inline-alert warning" style={{ marginTop: 14 }}>
        <ShieldCheck size={15} />每一次配置变更都由平台执行并记录证据；批准前不写 ECS，回滚后回到授权 profile 绑定的起点。
      </div>
    </div>
  </div>;
}

export function SystemOptimizationPage() {
  const capacities = useQuery({ queryKey: ['capacity-studies'], queryFn: api.capacityStudies });
  const targets = useQuery({ queryKey: ['targets'], queryFn: () => api.targets(false) });
  const completed = useMemo(() => capacities.data?.items.filter(item => item.status === 'completed') || [], [capacities.data]);
  const activeAlibaba = useMemo(() => targets.data?.items.filter(item => item.provider === 'alibaba' && item.lifecycleStatus === 'active') || [], [targets.data]);
  const [baselineId, setBaselineId] = useState('');
  const [targetId, setTargetId] = useState('');
  const [network, setNetwork] = useState<'internal' | 'external'>('internal');
  const [minimumEffect, setMinimumEffect] = useState('0.05');
  const [message, setMessage] = useState('等待运行');
  const [study, setStudy] = useState<SystemOptimizationStudy | null>(null);
  const [actionPending, setActionPending] = useState<string | null>(null);

  useEffect(() => {
    if (!completed.some(item => item.id === baselineId) && completed[0]) setBaselineId(completed[0].id);
    if (!activeAlibaba.some(item => item.id === targetId) && activeAlibaba[0]) setTargetId(activeAlibaba[0].id);
  }, [activeAlibaba, baselineId, completed, targetId]);

  const run = useMutation({
    mutationFn: async () => {
      setStudy(null);
      setMessage('读取已完成容量基线的 context digest…');
      const context = await api.systemOptimizationBaselineContext(baselineId, targetId, network);
      setMessage('已绑定容量上下文，开始在目标机采集只读 runtime profile（约 60 秒）…');
      const experiment = await api.createBenchmarkSmokeRun('looper.runtime.storage-profile', '0.1.3', {
        targetId,
        workloadId: 'storage-runtime',
        parameters: { duration_seconds: 60 },
        inputBindings: {
          'capacity-context': {
            kind: 'config',
            reference: `capacity-context://${context.contextDigest}`,
            digest: context.contextDigest,
            metadata: { capacityContextDigest: context.contextDigest, baselineCapacityStudyId: baselineId },
          },
        },
      });
      for (let attempt = 0; attempt < 140; attempt += 1) {
        const current = await api.experiment(experiment.id);
        setMessage(`runtime profile 运行中：${current.status}（${Math.min(99, Math.round((attempt / 140) * 100))}%）`);
        if (['completed', 'failed', 'cancelled'].includes(current.status)) {
          if (current.status !== 'completed') throw new Error(`runtime profile ${current.status}`);
          break;
        }
        await sleep(3000);
      }
      const profile = await api.systemOptimizationRuntimeProfile(experiment.id);
      setMessage('runtime profile 已入库，创建目标绑定授权 profile…');
      const authorization = await api.createSystemOptimizationAuthorizationProfile(targetId, profile.digest);
      setMessage('证据已绑定，生成可审计 hypothesis…');
      const created = await api.createSystemOptimizationStudy({
        baselineCapacityStudyId: baselineId,
        targetId,
        network,
        minimumEffect: Number(minimumEffect),
        authorizationProfileDigest: authorization.digest,
        runtimeProfileDigest: profile.digest,
      });
      return created;
    },
    onSuccess: value => { setStudy(value); setMessage(value.status === 'needs-attention' ? '研究创建完成，但进入了需要关注状态；见问题详情。' : '研究已创建，停在待批准状态；没有写入 ECS。'); },
    onError: error => setMessage(error instanceof Error ? error.message : '运行失败'),
  });

  const action = useMutation({
    mutationFn: async (kind: 'approve' | 'activate' | 'rollback') => {
      if (!study) throw new Error('没有可操作的研究');
      setActionPending(kind);
      if (kind === 'approve') return api.approveSystemOptimizationStudy(study.id);
      if (kind === 'activate') return api.activateSystemOptimizationStudy(study.id);
      return api.rollbackSystemOptimizationStudy(study.id);
    },
    onSuccess: (value, kind) => {
      setStudy(value);
      setActionPending(null);
      setMessage(kind === 'approve' ? '已批准，平台开始应用配置并复测。' : kind === 'rollback' ? '回滚请求已提交。' : '激活请求已提交。');
    },
    onError: (error, kind) => {
      setActionPending(null);
      setMessage(`${kind} 失败：${error instanceof Error ? error.message : String(error)}`);
    },
  });

  const loading = capacities.isLoading || targets.isLoading;
  return <div className="page narrow-page system-optimization-page">
    <PageHeader title="系统配置优化" description="基于已完成容量证据和目标机只读 runtime profile，生成一条需要人工批准的配置 hypothesis，并全程记录可回放证据。" />
    {loading ? <LoadingState label="正在读取基线和目标机" /> : capacities.isError || targets.isError ? <ErrorState error={capacities.error || targets.error} /> : <>
      <section className="panel">
        <div className="panel-heading"><div><h2>证据绑定</h2><p>每个 digest 都来自本地数据库或目标机实际观测，不接受未绑定的 profile。</p></div><ShieldCheck size={18} color="#2878c7" /></div>
        {!completed.length && <div className="inline-alert warning" style={{ margin: 18 }}><AlertTriangle size={15} /><span>还没有已完成的容量研究——先到 <Link to="/capacity">容量研究</Link> 完成一个基线。</span></div>}
        {!activeAlibaba.length && <div className="inline-alert warning" style={{ margin: '0 18px 18px' }}><AlertTriangle size={15} /><span>还没有活跃的阿里云目标机——到 <Link to="/cloud/market">云市场</Link> 购买或导入一台。</span></div>}
        <div className="form-grid" style={{ padding: 18 }}>
          <label><span className="field-label">容量基线</span><select value={baselineId} onChange={event => setBaselineId(event.target.value)}><option value="" disabled>请选择…</option>{completed.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></label>
          <label><span className="field-label">目标 ECS</span><select value={targetId} onChange={event => setTargetId(event.target.value)}><option value="" disabled>请选择…</option>{activeAlibaba.map(item => <option key={item.id} value={item.id}>{item.name} · {item.fingerprint?.instance_type || 'Alibaba ECS'}</option>)}</select></label>
          <label><span className="field-label">容量网络</span><select value={network} onChange={event => setNetwork(event.target.value as 'internal' | 'external')}><option value="internal">内网</option><option value="external">公网</option></select></label>
          <label><span className="field-label">最小效果</span><input type="number" min="0" step="0.01" value={minimumEffect} onChange={event => setMinimumEffect(event.target.value)} /></label>
        </div>
        <div style={{ padding: '0 18px 18px' }}><button className="button primary" disabled={run.isPending || !completed.length || !activeAlibaba.length || !baselineId || !targetId} onClick={() => run.mutate()}><WandSparkles size={15} />{run.isPending ? '正在运行配置优化…' : '运行配置优化'}</button></div>
      </section>
      <section className={`panel optimization-run-status ${run.isError ? 'error' : study ? 'success' : ''}`} style={{ marginTop: 18 }}>
        <div className="panel-heading"><div><h2>运行状态</h2><p>{message}</p></div>{run.isPending || actionPending ? <LoaderCircle className="spin" size={18} /> : run.isError ? <AlertTriangle size={18} color="#b53b42" /> : study ? <CheckCircle2 size={18} color="#2e9a64" /> : <Activity size={18} color="#7b8793" />}</div>
        {study ? <StudyDetail study={study} message={message} onAction={kind => action.mutate(kind)} actionPending={actionPending} /> :
          <div style={{ padding: 18, color: '#7b8793', fontSize: 12 }}>尚无研究结果。完成上方“证据绑定”并运行后，研究状态、证据工件与批准/回滚操作会显示在这里。</div>}
      </section>
    </>}
  </div>;
}
