import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import {
  Activity, AlertTriangle, CheckCircle2, ChevronDown, FileCheck2, LoaderCircle,
  RotateCcw, Settings2, ShieldCheck, WandSparkles,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import { PageHeader } from '../components/PageHeader';
import { ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';
import type { SystemOptimizationManifestItem, SystemOptimizationStudy } from '../lib/types';

const STAGES = ['baseline', 'hypothesis', 'apply', 'measure', 'evaluate', 'restore'] as const;
const STAGE_LABEL: Record<string, string> = {
  baseline: '基线', hypothesis: '假设', apply: '干预', measure: '复测', evaluate: '裁决', restore: '恢复',
};
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
const STATUS_STAGE: Record<string, number> = {
  draft: 0, 'hypothesis-ready': 1, 'awaiting-approval': 1, applying: 2, measuring: 3,
  'rolling-back': 5, evaluating: 4, completed: 5, 'needs-attention': 5,
};
const RISK_LABEL: Record<string, string> = { low: '低', medium: '中', high: '高' };

function sleep(ms: number) { return new Promise(resolve => window.setTimeout(resolve, ms)); }

function StudyStatusBadge({ status }: { status: string }) {
  return <span className={`status ${STATUS_CLASS[status] || 'status-paused'}`}><span />{STATUS_LABEL[status] || status}</span>;
}

function StageIndicator({ status }: { status: string }) {
  const active = STATUS_STAGE[status] ?? 0;
  return <div className="tuning-stages">
    {STAGES.map((stage, index) => <span key={stage} className={index < active ? 'done' : index === active ? 'active' : ''}>
      <i>{index + 1}</i><b>{STAGE_LABEL[stage]}</b>
    </span>)}
  </div>;
}

function LogTimeline({ study }: { study: SystemOptimizationStudy }) {
  const phases = Object.entries(study.orchestration || {})
    .filter(([key]) => !['schema_version', 'allowed_config_items'].includes(key))
    .map(([key, value]) => ({ key, phase: value && typeof value === 'object' && 'phase' in value ? String((value as Record<string, unknown>).phase ?? '') : 'recorded' }));
  const entries: Array<{ label: string; detail: string; tone: 'ok' | 'warn' | 'info' | 'bad' }> = [];
  const push = (label: string, detail: string, tone: 'ok' | 'warn' | 'info' | 'bad' = 'info') => entries.push({ label, detail, tone });
  if (study.createdAt) push('研究创建', `${study.baselineCapacityStudyId} → ${study.targetId}`, 'info');
  phases.forEach(item => push(`阶段 · ${item.key}`, `状态：${item.phase}`, item.phase === 'verified' || item.phase === 'completed' ? 'ok' : item.phase === 'failed' ? 'bad' : 'info'));
  if (study.hypothesisDigest) push('假设生成', study.hypothesisDigest, 'ok');
  if (study.approvedAt) push('人工批准', `批准于 ${study.approvedAt}`, 'ok');
  if (study.decisionDigest) push('裁决', study.decisionDigest, 'info');
  study.artifacts.forEach(artifact => push(`证据 · ${artifact.role}`, `${artifact.name}（${artifact.digest}）`, 'ok'));
  if (study.problem) push('问题', `${study.problem.message}${study.problem.suggestedAction ? ` · 建议：${study.problem.suggestedAction}` : ''}`, 'bad');
  if (study.completedAt) push('研究完成', `完成于 ${study.completedAt}`, 'ok');
  if (!entries.length) push('尚无记录', '运行后每一步动作、证据与裁决会出现在这里。', 'info');
  return <div className="tuning-log">
    {entries.map((entry, index) => <div key={index} className={`tuning-log-row ${entry.tone}`}>
      <span className="tuning-log-dot" /><div><strong>{entry.label}</strong><p>{entry.detail}</p></div>
    </div>)}
  </div>;
}

function AdvancedOptions({ items, selected, onToggle }: {
  items: SystemOptimizationManifestItem[];
  selected: string[];
  onToggle: (id: string, enabled: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  return <div className="advanced-options">
    <button className="advanced-toggle" onClick={() => setOpen(value => !value)}>
      <Settings2 size={14} />高级选项 · 手动选择允许修改的配置策略<ChevronDown size={14} className={open ? 'rotate' : ''} />
    </button>
    {open && <div className="advanced-body">
      <p className="advanced-note">策略决定"开始调优"后引擎可以触碰哪些系统配置。未勾选的项在任何情况下都不会被修改；本清单为内置默认清单，运行只读诊断后以目标机实测域为准并绑定进授权 profile。</p>
      {items.map(item => {
        const enabled = selected.includes(item.id);
        return <div key={item.id} className="config-item-row">
          <label className="switch"><input type="checkbox" checked={enabled} onChange={event => onToggle(item.id, event.target.checked)} /><span /></label>
          <div className="config-item-main">
            <strong>{item.id}</strong>
            <small>{item.description}</small>
            <code>{item.target}</code>
          </div>
          <div className="config-item-meta">
            <span className={`risk risk-${item.risk}`}>{RISK_LABEL[item.risk] || item.risk}</span>
            <small>可选值：{item.choices.join(' / ')}</small>
          </div>
        </div>;
      })}
    </div>}
  </div>;
}

export function SystemOptimizationPage() {
  const capacities = useQuery({ queryKey: ['capacity-studies'], queryFn: api.capacityStudies });
  const targets = useQuery({ queryKey: ['targets'], queryFn: () => api.targets(false) });
  const manifest = useQuery({ queryKey: ['system-optimization-manifest'], queryFn: api.systemOptimizationManifest });
  const completed = useMemo(() => capacities.data?.items.filter(item => item.status === 'completed') || [], [capacities.data]);
  const activeAlibaba = useMemo(() => targets.data?.items.filter(item => item.provider === 'alibaba' && item.lifecycleStatus === 'active') || [], [targets.data]);
  const [baselineId, setBaselineId] = useState('');
  const [targetId, setTargetId] = useState('');
  const [network, setNetwork] = useState<'internal' | 'external'>('internal');
  const [minimumEffect, setMinimumEffect] = useState('0.05');
  const [message, setMessage] = useState('等待启动');
  const [study, setStudy] = useState<SystemOptimizationStudy | null>(null);
  const [actionPending, setActionPending] = useState<string | null>(null);
  const [allowedItems, setAllowedItems] = useState<string[]>([]);
  const manifestItems = manifest.data?.items || [];

  useEffect(() => {
    if (!completed.some(item => item.id === baselineId) && completed[0]) setBaselineId(completed[0].id);
    if (!activeAlibaba.some(item => item.id === targetId) && activeAlibaba[0]) setTargetId(activeAlibaba[0].id);
  }, [activeAlibaba, baselineId, completed, targetId]);

  useEffect(() => {
    if (manifestItems.length && !allowedItems.length) setAllowedItems(manifestItems.map(item => item.id));
  }, [allowedItems.length, manifestItems]);

  const toggleItem = (id: string, enabled: boolean) => {
    setAllowedItems(current => enabled ? Array.from(new Set([...current, id])) : current.filter(value => value !== id));
  };

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
        allowedConfigItemIds: allowedItems,
      });
      return created;
    },
    onSuccess: value => { setStudy(value); setMessage(value.status === 'needs-attention' ? '研究创建完成，但进入了需要关注状态；见日志。' : '研究已创建，停在待批准状态；没有写入 ECS。'); },
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

  const running = run.isPending || !!actionPending;
  const loading = capacities.isLoading || targets.isLoading;
  const canRun = !running && completed.length > 0 && activeAlibaba.length > 0 && !!baselineId && !!targetId;
  const canApprove = study?.status === 'awaiting-approval';
  const canRollback = !!study && (['applying', 'measuring', 'evaluating'].includes(study.status)
    || (study.status === 'needs-attention' && (study.orchestration?.rollback as { phase?: string } | undefined)?.phase !== 'verified'));

  return <div className="page narrow-page system-optimization-page">
    <PageHeader title="系统配置优化" description="面向具体业务场景：测量真实负载表现，只修改系统配置（不触碰业务代码），每一步都留下可回放证据。" />
    {loading ? <LoadingState label="正在读取基线和目标机" /> : capacities.isError || targets.isError ? <ErrorState error={capacities.error || targets.error} /> : <>
      <section className="panel">
        <div className="panel-heading"><div><h2>场景与授权</h2><p>选定要优化的业务场景与目标机器，圈定允许修改的系统配置范围。</p></div><ShieldCheck size={18} color="#2878c7" /></div>
        {!completed.length && <div className="inline-alert" style={{ margin: 18 }}><AlertTriangle size={15} /><span>还没有已完成的容量研究（业务场景基线）——先到 <Link to="/capacity">容量研究</Link> 完成一个基线。</span></div>}
        {!activeAlibaba.length && <div className="inline-alert" style={{ margin: '0 18px 18px' }}><AlertTriangle size={15} /><span>还没有活跃的阿里云目标机——到 <Link to="/cloud/market">云市场</Link> 购买或导入一台。</span></div>}
        <div className="form-grid" style={{ padding: 18 }}>
          <label><span className="field-label">业务场景（容量基线）</span><select value={baselineId} onChange={event => setBaselineId(event.target.value)}><option value="" disabled>请选择…</option>{completed.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></label>
          <label><span className="field-label">目标 ECS</span><select value={targetId} onChange={event => setTargetId(event.target.value)}><option value="" disabled>请选择…</option>{activeAlibaba.map(item => <option key={item.id} value={item.id}>{item.name} · {item.fingerprint?.instance_type || 'Alibaba ECS'}</option>)}</select></label>
          <label><span className="field-label">容量网络</span><select value={network} onChange={event => setNetwork(event.target.value as 'internal' | 'external')}><option value="internal">内网</option><option value="external">公网</option></select></label>
          <label><span className="field-label">最小效果</span><input type="number" min="0" step="0.01" value={minimumEffect} onChange={event => setMinimumEffect(event.target.value)} /></label>
        </div>
        <div style={{ padding: '0 18px' }}><AdvancedOptions items={manifestItems} selected={allowedItems} onToggle={toggleItem} /></div>
        <div className="tuning-launch">
          <div><strong>开始调优</strong><p>{canRun ? '关闭开关不会中断已授权运行；正在进行的相位按合同自动恢复。' : '打开后按所选策略启动一轮完整调优：基线 → 假设 → 干预 → 复测 → 裁决 → 恢复。'}</p></div>
          <label className="launch-switch"><input type="checkbox" checked={running} disabled={!canRun && !running} onChange={event => { if (event.target.checked) run.mutate(); }} /><span><WandSparkles size={16} />{running ? '调优中…' : '启动'}</span></label>
        </div>
      </section>
      <section className={`panel optimization-run-status ${run.isError ? 'error' : study ? 'success' : ''}`} style={{ marginTop: 18 }}>
        <div className="panel-heading"><div><h2>调优运行</h2><p>{message}</p></div>{running ? <LoaderCircle className="spin" size={18} /> : run.isError ? <AlertTriangle size={18} color="#b53b42" /> : study ? <CheckCircle2 size={18} color="#2e9a64" /> : <Activity size={18} color="#7b8793" />}</div>
        {study && <>
          <div style={{ padding: '0 18px' }}><StageIndicator status={study.status} /></div>
          <div style={{ padding: 18 }}>
            <dl className="key-value-list">
              <div><dt>研究 ID</dt><dd><code>{study.id}</code></dd></div>
              <div><dt>状态</dt><dd><StudyStatusBadge status={study.status} /></dd></div>
              <div><dt>目标</dt><dd><code>{study.targetId}</code></dd></div>
              <div><dt>最小效果</dt><dd>{study.minimumEffect}</dd></div>
              <div><dt>Hypothesis</dt><dd><code>{study.hypothesisDigest || '尚未生成'}</code></dd></div>
              <div><dt>决策</dt><dd><code>{study.decisionDigest || '尚未裁决'}</code></dd></div>
              <div><dt>允许修改的配置项</dt><dd>{study.orchestration && 'allowed_config_items' in study.orchestration && Array.isArray(study.orchestration.allowed_config_items) ? (study.orchestration.allowed_config_items as string[]).join('、') || '（空——策略未勾选任何配置项）' : '未记录'}</dd></div>
            </dl>
            {study.artifacts.length > 0 && <div style={{ marginTop: 16 }}>
              <p className="field-label">证据工件（{study.artifacts.length}）</p>
              <ul className="system-optimization-artifacts">
                {study.artifacts.map(artifact => <li key={`${artifact.digest}-${artifact.role}-${artifact.name}`}>
                  <FileCheck2 size={13} /><span><strong>{artifact.role}</strong> {artifact.name}</span><code>{artifact.digest}</code>
                </li>)}
              </ul>
            </div>}
            <div style={{ display: 'flex', gap: 8, marginTop: 18 }}>
              {canApprove && <button className="button primary" disabled={!!actionPending} onClick={() => action.mutate('approve')}>
                <ShieldCheck size={15} />{actionPending === 'approve' ? '正在批准…' : '批准并开始应用'}
              </button>}
              {canRollback && <button className="button danger" disabled={!!actionPending} onClick={() => action.mutate('rollback')}>
                <RotateCcw size={15} />{actionPending === 'rollback' ? '正在回滚…' : '回滚配置'}
              </button>}
              {!canApprove && !canRollback && study.status !== 'completed' && study.status !== 'needs-attention' &&
                <span className="system-optimization-note">平台正在执行编排流程，页面自动跟随状态变化；无需操作。</span>}
            </div>
            <div className="inline-alert" style={{ marginTop: 14, marginBottom: 0 }}><ShieldCheck size={15} /><span>每一次配置变更都由平台执行并记录证据；批准前不写 ECS，回滚后回到授权 profile 绑定的起点。</span></div>
          </div>
        </>}
        {!study && <div style={{ padding: 18, color: '#7b8793', fontSize: 12 }}>尚无运行结果。配置上方场景并打开"开始调优"开关后，运行状态、阶段进度与批准/回滚操作会显示在这里。</div>}
      </section>
      <section className="panel" style={{ marginTop: 18 }}>
        <div className="panel-heading"><div><h2>运行日志</h2><p>基于研究记录输出：每一步动作、阶段状态、证据摘要与裁决。</p></div><FileCheck2 size={18} color="#7b8793" /></div>
        {study ? <div style={{ padding: 18 }}><LogTimeline study={study} /></div> :
          <div style={{ padding: 18, color: '#7b8793', fontSize: 12 }}>运行后，日志会按时间线记录研究创建、各阶段状态、假设、批准、证据与裁决。</div>}
      </section>
    </>}
  </div>;
}
