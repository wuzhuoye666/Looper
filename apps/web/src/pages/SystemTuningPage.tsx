import { useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleDashed,
  FileCheck2,
  Gauge,
  RotateCcw,
  ShieldCheck,
  Square,
} from 'lucide-react';
import {
  DEMO_CONFIG_ITEMS,
  DEMO_FRAMES,
  DEMO_TARGETS,
  EMPTY_METRICS,
  RESTORE_FRAMES,
  TUNING_STAGES,
  formatOps,
  type TuningHypothesisView,
  type TuningLogEvent,
  type TuningMetrics,
} from '../lib/tuningDemo';

type RunStatus = 'idle' | 'running' | 'needs-approval' | 'restoring' | 'completed';
type Decision = 'approved' | 'rejected' | 'stopped' | null;

const STAGE_ICONS = [Gauge, CircleDashed, ShieldCheck, Gauge, CheckCircle2, RotateCcw];

const STATUS_LABELS: Record<RunStatus, string> = {
  idle: '待配置',
  running: '运行中',
  'needs-approval': '待批准',
  restoring: '还原现场',
  completed: '已完成',
};

function nowLabel(at: number): string {
  const date = new Date(at);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

const LEVEL_CLASS: Record<TuningLogEvent['level'], string> = {
  info: 'tuning-log-info',
  success: 'tuning-log-success',
  warn: 'tuning-log-warn',
  error: 'tuning-log-error',
};

const OUTCOME_BADGE: Record<TuningHypothesisView['outcome'], { label: string; cls: string }> = {
  pending: { label: '排队', cls: 'hyp-pending' },
  running: { label: '验证中', cls: 'hyp-running' },
  accepted: { label: '采纳', cls: 'hyp-accepted' },
  rejected: { label: '拒绝', cls: 'hyp-rejected' },
};

const RISK_CLASS: Record<string, string> = { 低: 'low', 中: 'mid', 高: 'high' };

export function SystemTuningPage() {
  // ②区：调优对象
  const [targetId, setTargetId] = useState('');
  const [workloadName, setWorkloadName] = useState('');
  const [workloadCommand, setWorkloadCommand] = useState('');
  const [metricName, setMetricName] = useState('吞吐');
  const [metricDir, setMetricDir] = useState('maximize');
  const [slo, setSlo] = useState('');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [enabledItems, setEnabledItems] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(DEMO_CONFIG_ITEMS.map((item) => [item.id, false])),
  );
  const [mdePct, setMdePct] = useState('2');
  const [degradationPct, setDegradationPct] = useState('2');
  const [maxInterventions, setMaxInterventions] = useState('2');
  const [budgetMinutes, setBudgetMinutes] = useState('40');

  // ③④⑤区：运行状态
  const [status, setStatus] = useState<RunStatus>('idle');
  const [stageIndex, setStageIndex] = useState(-1);
  const [frameIndex, setFrameIndex] = useState(0);
  const [logs, setLogs] = useState<TuningLogEvent[]>([]);
  const [metrics, setMetrics] = useState<TuningMetrics>(EMPTY_METRICS);
  const [hypotheses, setHypotheses] = useState<TuningHypothesisView[]>([]);
  const [decision, setDecision] = useState<Decision>(null);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  const enabledCount = useMemo(
    () => Object.values(enabledItems).filter(Boolean).length,
    [enabledItems],
  );
  const requirementsMet =
    targetId !== '' && workloadName.trim() !== '' && workloadCommand.trim() !== '' && enabledCount >= 1;
  const missingHints = useMemo(() => {
    const hints: string[] = [];
    if (targetId === '') hints.push('选择目标机器');
    if (workloadName.trim() === '') hints.push('填写业务负载名称');
    if (workloadCommand.trim() === '') hints.push('填写负载运行命令');
    if (enabledCount < 1) hints.push('在高级选项中授权至少一个可修改配置项');
    return hints;
  }, [targetId, workloadName, workloadCommand, enabledCount]);

  const appendLogs = (events: Array<{ stage: TuningLogEvent['stage']; level: TuningLogEvent['level']; text: string }>) => {
    const at = Date.now();
    setLogs((prev) => [...prev, ...events.map((event) => ({ ...event, at }))]);
  };

  // 演示运行主循环：按帧推进阶段、追加日志、更新指标
  useEffect(() => {
    if (status !== 'running') return;
    const timer = window.setInterval(() => {
      setFrameIndex((index) => {
        const frame = DEMO_FRAMES[index];
        if (!frame) return index;
        setStageIndex(frame.stageIndex);
        appendLogs(frame.logs);
        if (frame.metrics) setMetrics((prev) => ({ ...prev, ...frame.metrics }));
        if (frame.hypotheses) setHypotheses(frame.hypotheses);
        const next = index + 1;
        if (next >= DEMO_FRAMES.length) {
          window.clearInterval(timer);
          setStatus('needs-approval');
        }
        return next;
      });
    }, 1200);
    return () => window.clearInterval(timer);
  }, [status]);

  // 还原阶段循环：批准/拒绝/停止后播放恢复帧
  useEffect(() => {
    if (status !== 'restoring') return;
    let index = 0;
    const timer = window.setInterval(() => {
      const frame = RESTORE_FRAMES[index];
      if (!frame) {
        window.clearInterval(timer);
        setStatus('completed');
        return;
      }
      setStageIndex(frame.stageIndex);
      appendLogs(frame.logs);
      index += 1;
    }, 900);
    return () => window.clearInterval(timer);
  }, [status]);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ block: 'end' });
  }, [logs]);

  const launch = () => {
    if (!requirementsMet || status !== 'idle') return;
    setStatus('running');
    setStageIndex(0);
    setFrameIndex(0);
    setLogs([]);
    setMetrics(EMPTY_METRICS);
    setHypotheses([]);
    setDecision(null);
    appendLogs([
      { stage: 'baseline', level: 'info', text: `调优任务启动：目标 ${targetId}，主指标 ${metricName}（${metricDir === 'maximize' ? '越大越好' : '越小越好'}）` },
      { stage: 'baseline', level: 'info', text: `授权可改配置项 ${enabledCount} 个；MDE ${mdePct}%，退化红线 ${degradationPct}%，最大干预 ${maxInterventions} 次，预算 ${budgetMinutes} 分钟` },
    ]);
  };

  const finishWithDecision = (choice: Exclude<Decision, null>) => {
    if (status !== 'running' && status !== 'needs-approval') return;
    setDecision(choice);
    if (choice === 'approved') {
      appendLogs([{ stage: 'verdict', level: 'success', text: '用户批准：候选生成推荐配置（推荐与持久变更分离，现场仍恢复原状）' }]);
    } else if (choice === 'rejected') {
      appendLogs([{ stage: 'verdict', level: 'warn', text: '用户拒绝：候选不进入推荐' }]);
      setHypotheses((prev) =>
        prev.map((hyp) => (hyp.outcome === 'accepted' ? { ...hyp, outcome: 'rejected', detail: '用户拒绝采纳' } : hyp)),
      );
    } else {
      appendLogs([{ stage: 'restore', level: 'warn', text: '用户停止：按安全纪律先还原现场再结束' }]);
    }
    setStatus('restoring');
  };

  const resetAll = () => {
    setStatus('idle');
    setStageIndex(-1);
    setFrameIndex(0);
    setLogs([]);
    setMetrics(EMPTY_METRICS);
    setHypotheses([]);
    setDecision(null);
  };

  const busy = status === 'running' || status === 'restoring';
  const target = DEMO_TARGETS.find((item) => item.id === targetId);

  return (
    <div className="page">
      {/* ① 定位区 */}
      <div className="tuning-hero panel">
        <div className="tuning-hero-copy">
          <h1>业务系统调优</h1>
          <p>在你的业务负载下实测，<strong>只修改系统配置，不碰业务代码</strong>；基线由业务负载在目标机上自测产生，调优结束自动还原现场，每一步留下可回放证据。</p>
          <div className="tuning-badges">
            <span><ShieldCheck size={13} />只改系统配置</span>
            <span><Gauge size={13} />业务负载自测基线</span>
            <span><RotateCcw size={13} />结束自动还原</span>
            <span><FileCheck2 size={13} />证据可回放</span>
          </div>
        </div>
        <div className={`status tuning-status ${status === 'running' ? 'status-running' : status === 'completed' ? 'status-completed' : status === 'needs-approval' ? 'status-paused' : ''}`}>
          <span />{STATUS_LABELS[status]}
        </div>
      </div>

      {/* ② 调优对象区 */}
      <section className="panel tuning-section">
        <div className="panel-heading">
          <div>
            <h2>调优对象</h2>
            <p>选定目标机器与业务负载；懂行的用户可在高级选项里圈定允许修改的配置范围。</p>
          </div>
        </div>
        <div className="tuning-section-body">
          <div className="form-grid">
            <label>
              <span>目标机器 *</span>
              <select value={targetId} disabled={busy} onChange={(event) => setTargetId(event.target.value)}>
                <option value="" disabled>请选择…</option>
                {DEMO_TARGETS.map((item) => (
                  <option key={item.id} value={item.id}>{item.name} · {item.spec}</option>
                ))}
              </select>
            </label>
            <label>
              <span>业务负载名称 *</span>
              <input value={workloadName} disabled={busy} placeholder="例如：订单导入批处理" onChange={(event) => setWorkloadName(event.target.value)} />
            </label>
            <label className="full">
              <span>负载运行命令 *（身份摘要将绑定基线与复测，命令变了即视为不同负载）</span>
              <input value={workloadCommand} disabled={busy} placeholder="例如：python importer.py --workers 4 --batch 500" onChange={(event) => setWorkloadCommand(event.target.value)} />
            </label>
            <label>
              <span>主指标</span>
              <select value={metricName} disabled={busy} onChange={(event) => setMetricName(event.target.value)}>
                <option value="吞吐">吞吐</option>
                <option value="延迟">延迟</option>
                <option value="完成时间">完成时间</option>
                <option value="错误率">错误率</option>
              </select>
            </label>
            <label>
              <span>指标方向</span>
              <select value={metricDir} disabled={busy} onChange={(event) => setMetricDir(event.target.value)}>
                <option value="maximize">越大越好</option>
                <option value="minimize">越小越好</option>
              </select>
            </label>
            <label className="full">
              <span>SLO（可选，达标即停）</span>
              <input value={slo} disabled={busy} placeholder="例如：吞吐 ≥ 50000 ops/s" onChange={(event) => setSlo(event.target.value)} />
            </label>
          </div>

          <button type="button" className="tuning-advanced-toggle" disabled={busy} onClick={() => setAdvancedOpen((open) => !open)}>
            {advancedOpen ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
            高级选项（可选）：授权配置范围与实验参数
          </button>
          {advancedOpen && (
            <div className="tuning-advanced">
              <div className="tuning-advanced-label">允许修改的配置项（至少一项，未勾选的绝不触碰）</div>
              <div className="tuning-config-list">
                {DEMO_CONFIG_ITEMS.map((item) => (
                  <label key={item.id} className={`tuning-config-row ${enabledItems[item.id] ? 'on' : ''}`}>
                    <input
                      type="checkbox"
                      checked={Boolean(enabledItems[item.id])}
                      disabled={busy}
                      onChange={(event) => setEnabledItems((prev) => ({ ...prev, [item.id]: event.target.checked }))}
                    />
                    <span className="tuning-switch" aria-hidden="true"><i /></span>
                    <span className="tuning-config-copy">
                      <strong>{item.name}</strong>
                      <small>{item.description}</small>
                    </span>
                    <em className="tuning-component-tag">{item.component}</em>
                    <em className={`tuning-risk risk-${RISK_CLASS[item.risk]}`}>{item.risk}风险</em>
                  </label>
                ))}
              </div>
              <div className="tuning-params">
                <label>最小提升 MDE（%）
                  <input type="number" min="0" step="0.5" value={mdePct} disabled={busy} onChange={(event) => setMdePct(event.target.value)} />
                </label>
                <label>退化红线（%）
                  <input type="number" min="0" step="0.5" value={degradationPct} disabled={busy} onChange={(event) => setDegradationPct(event.target.value)} />
                </label>
                <label>最大干预次数
                  <input type="number" min="1" step="1" value={maxInterventions} disabled={busy} onChange={(event) => setMaxInterventions(event.target.value)} />
                </label>
                <label>时间预算（分钟）
                  <input type="number" min="1" step="1" value={budgetMinutes} disabled={busy} onChange={(event) => setBudgetMinutes(event.target.value)} />
                </label>
              </div>
            </div>
          )}
        </div>
      </section>

      {/* ③ 启动区 */}
      <section className="panel tuning-section tuning-launch-panel">
        <div className="tuning-launch-row">
          <label className={`tuning-launch ${requirementsMet && status === 'idle' ? '' : 'off'}`}>
            <input
              type="checkbox"
              checked={status !== 'idle'}
              disabled={!requirementsMet || busy || status === 'needs-approval' || status === 'completed'}
              onChange={(event) => (event.target.checked ? launch() : finishWithDecision('stopped'))}
            />
            <span className="tuning-switch big" aria-hidden="true"><i /></span>
            <span className="tuning-launch-copy">
              <strong>启动调优</strong>
              <small>{status === 'idle' ? '由你启动，不会自动开始' : `当前状态：${STATUS_LABELS[status]}`}</small>
            </span>
          </label>
          {busy && (
            <button type="button" className="button" onClick={() => finishWithDecision('stopped')}>
              <Square size={14} />停止并还原
            </button>
          )}
          {status === 'completed' && (
            <button type="button" className="button" onClick={resetAll}>
              <RotateCcw size={14} />重置
            </button>
          )}
        </div>
        {status === 'idle' && !requirementsMet && (
          <div className="tuning-missing">
            <AlertTriangle size={14} />
            <span>还差：{missingHints.join('；')}。</span>
          </div>
        )}
        {target && status !== 'idle' && (
          <div className="tuning-context-line">
            目标 <strong>{target.name}</strong> · 负载 <strong>{workloadName}</strong> · 主指标 <strong>{metricName}（{metricDir === 'maximize' ? '越大越好' : '越小越好'}）</strong>
          </div>
        )}
      </section>

      {/* ④ 运行过程区 */}
      <section className="panel tuning-section">
        <div className="panel-heading">
          <div>
            <h2>运行过程</h2>
            <p>基线 → 假设 → 干预 → 复测 → 裁决 → 还原；单相位只做一次变更，拒绝与接受都是有效结果。</p>
          </div>
        </div>
        <div className="tuning-section-body">
          <div className="tuning-stages">
            {TUNING_STAGES.map((stage, index) => {
              const state = stageIndex > index ? 'done' : stageIndex === index ? 'current' : 'todo';
              const Icon = STAGE_ICONS[index] ?? Gauge;
              return (
                <div key={stage.id} className={`tuning-stage ${state}`}>
                  <span className="tuning-stage-icon"><Icon size={15} /></span>
                  <strong>{stage.label}</strong>
                  <small>{stage.hint}</small>
                </div>
              );
            })}
          </div>

          <div className="tuning-metrics">
            <div><span>基线中位数</span><strong>{formatOps(metrics.baselineMedian)} <i>ops/s</i></strong></div>
            <div><span>复测中位数</span><strong>{formatOps(metrics.candidateMedian)} <i>ops/s</i></strong></div>
            <div className={metrics.improvementPct !== null && metrics.improvementPct > 0 ? 'good' : ''}>
              <span>提升幅度</span>
              <strong>{metrics.improvementPct === null ? '—' : `${metrics.improvementPct > 0 ? '+' : ''}${metrics.improvementPct.toFixed(2)}%`}</strong>
            </div>
            <div><span>基线波动 CV</span><strong>{metrics.cvPct === null ? '—' : `${metrics.cvPct.toFixed(2)}%`}</strong></div>
            <div className={metrics.lcbPct !== null ? (metrics.lcbPct > Number(mdePct) ? 'good' : 'bad') : ''}>
              <span>置信下界 vs MDE</span>
              <strong>{metrics.lcbPct === null ? '—' : `LCB ${metrics.lcbPct.toFixed(1)}% / ${mdePct}%`}</strong>
            </div>
          </div>

          {hypotheses.length > 0 && (
            <div className="tuning-hypotheses">
              <div className="tuning-advanced-label">竞争假设（同一症状至少登记两条才允许干预）</div>
              {hypotheses.map((hyp) => {
                const badge = OUTCOME_BADGE[hyp.outcome];
                return (
                  <div key={hyp.id} className="tuning-hypothesis">
                    <div className="tuning-hypothesis-head">
                      <strong>{hyp.title}</strong>
                      <em className={badge.cls}>{badge.label}</em>
                    </div>
                    <code>{hyp.changeText}</code>
                    <p>{hyp.detail}</p>
                  </div>
                );
              })}
            </div>
          )}

          {status === 'needs-approval' && (
            <div className="tuning-approval">
              <div className="tuning-approval-copy">
                <AlertTriangle size={16} />
                <div>
                  <strong>候选等待你的批准</strong>
                  <p>批准 = 生成推荐配置（现场仍恢复原状，推荐与持久变更分离）；拒绝 = 候选不进入推荐。两者都会还原现场。</p>
                </div>
              </div>
              <div className="tuning-approval-actions">
                <button type="button" className="button tuning-approve-btn" onClick={() => finishWithDecision('approved')}>批准并生成推荐</button>
                <button type="button" className="button" onClick={() => finishWithDecision('rejected')}>拒绝</button>
              </div>
            </div>
          )}

          {status === 'completed' && (
            <div className="tuning-result">
              {decision === 'approved' && (
                <p className="tuning-result-line ok"><CheckCircle2 size={15} />调优完成：假设①已生成推荐配置（+15.59%），现场已恢复起点，零残留。</p>
              )}
              {decision === 'rejected' && (
                <p className="tuning-result-line"><RotateCcw size={15} />调优完成：候选被拒绝，保持默认配置也是有效结论；现场已恢复起点。</p>
              )}
              {decision === 'stopped' && (
                <p className="tuning-result-line"><Square size={14} />已按你的指令停止：先还原现场再结束，证据链已封存。</p>
              )}
            </div>
          )}
        </div>
      </section>

      {/* ⑤ 运行日志区 */}
      <section className="panel tuning-section">
        <div className="panel-heading">
          <div>
            <h2>运行日志</h2>
            <p>按记录输出，每条带阶段标签；证据链封存后可离线回放。</p>
          </div>
        </div>
        <div className="tuning-log">
          {logs.length === 0 && <div className="tuning-log-empty">尚无日志。配置调优对象并打开「启动调优」后，这里按时间线输出每一步。</div>}
          {logs.map((event, index) => (
            <div key={`${event.at}-${index}`} className="tuning-log-row">
              <time>{nowLabel(event.at)}</time>
              <em className={LEVEL_CLASS[event.level]}>{TUNING_STAGES.find((stage) => stage.id === event.stage)?.label ?? event.stage}</em>
              <span>{event.text}</span>
            </div>
          ))}
          <div ref={logEndRef} />
        </div>
      </section>
    </div>
  );
}
