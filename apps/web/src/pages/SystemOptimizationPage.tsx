import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { Activity, AlertTriangle, CheckCircle2, LoaderCircle, ShieldCheck, WandSparkles } from 'lucide-react';
import { PageHeader } from '../components/PageHeader';
import { ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';

const BASELINE_ID = 'capacity_610998e8eba047d194d0537c2443ff2c';
const TARGET_ID = 'cloud:alibaba:cn-hangzhou:i-bp11q16rufy3hemez1v8';

function sleep(ms: number) { return new Promise(resolve => window.setTimeout(resolve, ms)); }

export function SystemOptimizationPage() {
  const capacities = useQuery({ queryKey: ['capacity-studies'], queryFn: api.capacityStudies });
  const targets = useQuery({ queryKey: ['targets'], queryFn: () => api.targets(false) });
  const completed = useMemo(() => capacities.data?.items.filter(item => item.status === 'completed') || [], [capacities.data]);
  const activeAlibaba = useMemo(() => targets.data?.items.filter(item => item.provider === 'alibaba' && item.lifecycleStatus === 'active') || [], [targets.data]);
  const [baselineId, setBaselineId] = useState(BASELINE_ID);
  const [targetId, setTargetId] = useState(TARGET_ID);
  const [network, setNetwork] = useState<'internal' | 'external'>('internal');
  const [minimumEffect, setMinimumEffect] = useState('0.05');
  const [message, setMessage] = useState('等待运行');
  const [study, setStudy] = useState<Awaited<ReturnType<typeof api.systemOptimizationStudy>> | null>(null);

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
        setMessage(`runtime profile 运行中：${current.status}（${Math.min(99, Math.round((attempt / 120) * 100))}%）`);
        if (['completed', 'failed', 'cancelled'].includes(current.status)) {
          if (current.status !== 'completed') throw new Error(`runtime profile ${current.status}`);
          break;
        }
        await sleep(3000);
      }
      const profile = await api.systemOptimizationRuntimeProfile(experiment.id);
      setMessage('runtime profile 已入库，创建目标绑定授权 profile…');
      const authorization = await api.createSystemOptimizationAuthorizationProfile(targetId, profile.digest);
      setMessage('证据已绑定，调用 DeepSeek 生成可审计 hypothesis…');
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
    onSuccess: value => { setStudy(value); setMessage('研究已创建，当前停在待批准状态；没有写入 ECS。'); },
    onError: error => setMessage(error instanceof Error ? error.message : '运行失败'),
  });

  const loading = capacities.isLoading || targets.isLoading;
  return <div className="page narrow-page system-optimization-page">
    <PageHeader title="系统配置优化" description="基于已完成容量证据和目标机只读 runtime profile，生成一条需要人工批准的配置 hypothesis。" />
    {loading ? <LoadingState label="正在读取基线和目标机" /> : capacities.isError || targets.isError ? <ErrorState error={capacities.error || targets.error} /> : <>
      <section className="panel">
        <div className="panel-heading"><div><h2>证据绑定</h2><p>每个 digest 都来自本地数据库或目标机实际观测，不接受未绑定的 profile。</p></div><ShieldCheck size={18} color="#2878c7" /></div>
        <div className="form-grid" style={{ padding: 18 }}>
          <label><span className="field-label">容量基线</span><select value={baselineId} onChange={event => setBaselineId(event.target.value)}>{completed.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></label>
          <label><span className="field-label">目标 ECS</span><select value={targetId} onChange={event => setTargetId(event.target.value)}>{activeAlibaba.map(item => <option key={item.id} value={item.id}>{item.name} · {item.fingerprint?.instance_type || 'Alibaba ECS'}</option>)}</select></label>
          <label><span className="field-label">容量网络</span><select value={network} onChange={event => setNetwork(event.target.value as 'internal' | 'external')}><option value="internal">内网</option><option value="external">公网</option></select></label>
          <label><span className="field-label">最小效果</span><input type="number" min="0" step="0.01" value={minimumEffect} onChange={event => setMinimumEffect(event.target.value)} /></label>
        </div>
        <div style={{ padding: '0 18px 18px' }}><button className="button primary" disabled={run.isPending || !completed.length || !activeAlibaba.length} onClick={() => run.mutate()}><WandSparkles size={15} />{run.isPending ? '正在运行配置优化…' : '运行配置优化'}</button></div>
      </section>
      <section className={`panel optimization-run-status ${run.isError ? 'error' : study ? 'success' : ''}`} style={{ marginTop: 18 }}>
        <div className="panel-heading"><div><h2>运行状态</h2><p>{message}</p></div>{run.isPending ? <LoaderCircle className="spin" size={18} /> : run.isError ? <AlertTriangle size={18} color="#b53b42" /> : study ? <CheckCircle2 size={18} color="#2e9a64" /> : <Activity size={18} color="#7b8793" />}</div>
        {study && <div style={{ padding: 18 }}><dl className="key-value-list"><div><dt>研究 ID</dt><dd><code>{study.id}</code></dd></div><div><dt>状态</dt><dd><span className="status status-paused"><span />{study.status}</span></dd></div><div><dt>目标</dt><dd><code>{study.targetId}</code></dd></div><div><dt>授权 profile</dt><dd><code>{study.authorizationProfileDigest}</code></dd></div><div><dt>Hypothesis</dt><dd><code>{study.hypothesisDigest || '尚未生成'}</code></dd></div></dl><div className="inline-alert warning" style={{ marginTop: 14 }}><ShieldCheck size={15} />当前研究仅生成证据和 hypothesis，激活需要单独批准；页面没有执行 ECS 写入。</div></div>}
      </section>
    </>}
  </div>;
}
