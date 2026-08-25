// 业务系统调优页的演示数据与类型定义。
// 数字取自 2026-08-25 真机 discovery 实验（THP madvise→always，+15.59%），
// 当前仅用于页面演示；接入真实后端时替换本模块的数据来源，页面结构不变。

export type TuningStageId =
  | 'baseline'
  | 'hypothesis'
  | 'intervention'
  | 'retest'
  | 'verdict'
  | 'restore';

export interface TuningStage {
  id: TuningStageId;
  label: string;
  hint: string;
}

export const TUNING_STAGES: TuningStage[] = [
  { id: 'baseline', label: '基线采集', hint: '业务负载自测，冻结基线与波动' },
  { id: 'hypothesis', label: '观测假设', hint: '登记竞争瓶颈假设' },
  { id: 'intervention', label: '配置干预', hint: '先快照，单变更写入' },
  { id: 'retest', label: '复测对比', hint: '同一负载协议下复测' },
  { id: 'verdict', label: '裁决', hint: 'LCB > MDE 统计裁决' },
  { id: 'restore', label: '还原现场', hint: '结束无条件恢复起点' },
];

export interface TuningTargetOption {
  id: string;
  name: string;
  spec: string;
}

export const DEMO_TARGETS: TuningTargetOption[] = [
  { id: 'target-8vcpu', name: '业务机组 · 爬虫节点 01', spec: '8 vCPU / 16 GiB · Ubuntu 22.04 · 在线' },
  { id: 'target-2vcpu', name: '业务机组 · 灰度节点 02', spec: '2 vCPU / 2 GiB · Ubuntu 22.04 · 在线' },
];

export interface TuningConfigItem {
  id: string;
  name: string;
  component: 'CPU' | '内存' | '网络' | '存储';
  risk: '低' | '中' | '高';
  description: string;
}

export const DEMO_CONFIG_ITEMS: TuningConfigItem[] = [
  {
    id: 'thp',
    name: '透明大页 THP',
    component: '内存',
    risk: '中',
    description: 'transparent_hugepage/enabled（madvise / always / never）',
  },
  {
    id: 'swappiness',
    name: '交换倾向 swappiness',
    component: '内存',
    risk: '低',
    description: 'vm.swappiness（0–100，越小越少换出）',
  },
  {
    id: 'cpu-governor',
    name: '调频策略 governor',
    component: 'CPU',
    risk: '中',
    description: 'scaling_governor（powersave / performance / schedutil）',
  },
  {
    id: 'somaxconn',
    name: '连接队列 somaxconn',
    component: '网络',
    risk: '低',
    description: 'net.core.somaxconn（监听积压上限）',
  },
  {
    id: 'io-scheduler',
    name: 'IO 调度器',
    component: '存储',
    risk: '中',
    description: '块设备 IO scheduler（none / mq-deadline / bfq）',
  },
];

export type TuningLogLevel = 'info' | 'success' | 'warn' | 'error';

export interface TuningLogEvent {
  at: number;
  stage: TuningStageId;
  level: TuningLogLevel;
  text: string;
}

export interface TuningMetrics {
  baselineMedian: number | null;
  candidateMedian: number | null;
  improvementPct: number | null;
  cvPct: number | null;
  lcbPct: number | null;
}

export const EMPTY_METRICS: TuningMetrics = {
  baselineMedian: null,
  candidateMedian: null,
  improvementPct: null,
  cvPct: null,
  lcbPct: null,
};

export type HypothesisOutcome = 'pending' | 'running' | 'accepted' | 'rejected';

export interface TuningHypothesisView {
  id: string;
  title: string;
  changeText: string;
  rank: number;
  outcome: HypothesisOutcome;
  detail: string;
}

export interface DemoFrame {
  stageIndex: number;
  logs: Array<{ stage: TuningStageId; level: TuningLogLevel; text: string }>;
  metrics?: Partial<TuningMetrics>;
  hypotheses?: TuningHypothesisView[];
}

const HYPOTHESIS_ALWAYS: TuningHypothesisView = {
  id: 'hyp-thp-always',
  title: '假设① 透明大页全量启用',
  changeText: 'THP: madvise → always',
  rank: 1,
  outcome: 'pending',
  detail: '负载为连续大块内存写入，未显式声明大页的区域无法受益于 madvise',
};

const HYPOTHESIS_NEVER: TuningHypothesisView = {
  id: 'hyp-thp-never',
  title: '假设② 透明大页关闭',
  changeText: 'THP: madvise → never',
  rank: 2,
  outcome: 'pending',
  detail: '竞争假设：大页合并开销可能是负担，关闭后观察变化',
};

export const DEMO_FRAMES: DemoFrame[] = [
  {
    stageIndex: 0,
    logs: [
      { stage: 'baseline', level: 'info', text: '取得目标写租约，进入基线采集阶段' },
      { stage: 'baseline', level: 'info', text: '以业务负载身份起压：5 个观察窗口，逐窗记录主指标' },
    ],
  },
  {
    stageIndex: 0,
    logs: [
      { stage: 'baseline', level: 'info', text: '窗口 1/5：44871 ops/s · 窗口 2/5：44320 ops/s' },
    ],
  },
  {
    stageIndex: 0,
    logs: [
      { stage: 'baseline', level: 'info', text: '窗口 3/5：45102 ops/s · 窗口 4/5：44684 ops/s · 窗口 5/5：44513 ops/s' },
      { stage: 'baseline', level: 'success', text: '基线冻结：中位数 44684 ops/s，CV 0.65%（波动可控，测量力足够）' },
    ],
    metrics: { baselineMedian: 44684, cvPct: 0.65 },
  },
  {
    stageIndex: 1,
    logs: [
      { stage: 'hypothesis', level: 'info', text: '低开销系统指标观测：内存域压力与业务吞吐相关' },
      { stage: 'hypothesis', level: 'info', text: '登记 2 条竞争假设（单症状至少双假设才允许干预）' },
    ],
    hypotheses: [
      { ...HYPOTHESIS_ALWAYS, outcome: 'running' },
      HYPOTHESIS_NEVER,
    ],
  },
  {
    stageIndex: 2,
    logs: [
      { stage: 'intervention', level: 'info', text: '写入前快照已保存：THP = madvise' },
      { stage: 'intervention', level: 'warn', text: '施加假设①（本相位唯一变更）：THP: madvise → always' },
      { stage: 'intervention', level: 'success', text: '读回验证通过，变更生效；负载进程不受影响' },
    ],
  },
  {
    stageIndex: 3,
    logs: [
      { stage: 'retest', level: 'info', text: '复测窗口 1/5：51302 ops/s · 窗口 2/5：51844 ops/s' },
    ],
    metrics: { candidateMedian: null },
  },
  {
    stageIndex: 3,
    logs: [
      { stage: 'retest', level: 'info', text: '复测窗口 3/5：51649 ops/s · 窗口 4/5：51587 ops/s · 窗口 5/5：51710 ops/s' },
      { stage: 'retest', level: 'info', text: '复测中位数 51649 ops/s，与基线同协议可比' },
    ],
    metrics: { candidateMedian: 51649 },
  },
  {
    stageIndex: 4,
    logs: [
      { stage: 'verdict', level: 'info', text: '统计裁决：提升 +15.59%，bootstrap 置信下界 LCB = 12.4% > MDE = 2%' },
      { stage: 'verdict', level: 'success', text: '假设① 通过接受条件（LCB > MDE），退化门未触发' },
      { stage: 'verdict', level: 'info', text: '等待用户批准：批准后生成推荐配置；现场仍会恢复原状' },
    ],
    metrics: { improvementPct: 15.59, lcbPct: 12.4 },
    hypotheses: [
      { ...HYPOTHESIS_ALWAYS, outcome: 'accepted', detail: '+15.59%，LCB 12.4% > MDE 2%，通过' },
      { ...HYPOTHESIS_NEVER, outcome: 'pending', detail: '竞争假设保持排队：按单相位单变更纪律，留待下一相位' },
    ],
  },
];

export const RESTORE_FRAMES: DemoFrame[] = [
  {
    stageIndex: 5,
    logs: [
      { stage: 'restore', level: 'info', text: '相位收尾：无条件把配置恢复到相位起点' },
      { stage: 'restore', level: 'info', text: '恢复写入：THP: always → madvise' },
    ],
  },
  {
    stageIndex: 5,
    logs: [
      { stage: 'restore', level: 'success', text: '读回验证通过：THP = madvise，与相位起点一致（零残留）' },
      { stage: 'restore', level: 'success', text: '租约释放，证据链封存：本相位全部窗口、快照与裁决可离线回放' },
    ],
  },
];

// 用户显式授权"保留候选配置生效"时的收尾帧：不再写回起点，改为确认当前值并封存证据。
export const KEEP_FRAMES: DemoFrame[] = [
  {
    stageIndex: 5,
    logs: [
      { stage: 'restore', level: 'info', text: '相位收尾（用户已授权保留）：不写回相位起点' },
      { stage: 'restore', level: 'info', text: '读回确认当前生效值：THP = always（与批准的候选一致）' },
    ],
  },
  {
    stageIndex: 5,
    logs: [
      { stage: 'restore', level: 'success', text: '保留生效已登记：批准时刻成为该配置项的新基线，后续任务以此为参照' },
      { stage: 'restore', level: 'success', text: '租约释放，证据链封存：本相位全部窗口、快照与裁决可离线回放' },
    ],
  },
];

export function formatOps(value: number | null): string {
  if (value === null) return '—';
  return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
}
