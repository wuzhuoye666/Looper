import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { VariabilityPanel } from '../components/VariabilityPanel';
import { Evidence, experimentTabs, PhpBenchResultSection, SysbenchWorkloadSection, ValidityGatesSection } from '../pages/ExperimentDetailPage';
import type { Experiment } from '../lib/types';

describe('experiment result navigation', () => {
  it('uses universal tabs plus benchmark-declared sections', () => {
    const tabs = experimentTabs({
      id: 'exp-1',
      name: 'DCPerf',
      status: 'cancelled',
      mode: 'selection',
      resultSections: [
        { id: 'throughput-latency', label: '吞吐与延迟', metrics: ['wrk_rps'] },
        { id: 'validity-gates', label: '有效性门禁', metrics: ['failed_request_ratio'] },
      ],
    } satisfies Experiment);

    expect(tabs.map(([, label]) => label)).toEqual([
      '概览', '优化建议', '吞吐与延迟', '有效性门禁', '证据', '配置', '原始终端',
    ]);
    expect(tabs.flat()).not.toContain('对比结论');
    expect(tabs.flat()).not.toContain('可信度');
    expect(tabs.flat()).not.toContain('波动分析');
  });

  it('renders the benchmark-selected Sysbench workload view with collected metrics', () => {
    render(<SysbenchWorkloadSection
      section={{
        id: 'sysbench-workloads',
        label: 'Sysbench workload 数据',
        view: 'sysbench-workloads',
        metrics: ['events_per_sec', 'throughput_mib_s'],
      }}
      definitions={{
        events_per_sec: { unit: 'events/s', presentation: { userLabel: '每秒事件数', displayPrecision: 2 } },
        throughput_mib_s: { unit: 'MiB/s', presentation: { userLabel: '内存吞吐量', displayPrecision: 2 } },
      }}
      evaluations={[{
        id: 'eval-memory',
        candidate: '测试机3',
        workload: 'memory',
        status: 'completed',
        metrics: [
          { name: 'events_per_sec', value: 4576065.04, unit: 'events/s', sampleCount: 3, statistic: 'mean' },
          { name: 'throughput_mib_s', value: 4467.92, unit: 'MiB/s', sampleCount: 3, statistic: 'mean' },
        ],
      }]}
    />);

    expect(screen.getByRole('heading', { name: 'Sysbench workload 数据' })).toBeInTheDocument();
    expect(screen.getByText('内存吞吐')).toBeInTheDocument();
    expect(screen.getByText('4,467.92 MiB/s')).toBeInTheDocument();
    expect(screen.getByText('内存吞吐量（平均值）')).toBeInTheDocument();
    expect(screen.getByText('成功采集 3 次的算术平均')).toBeInTheDocument();
    expect(screen.getByText('界面显示平均值；逐次数据已写入证据')).toBeInTheDocument();
  });

  it('renders the PHPBench mean, parameters, gates, and evidence without per-run samples', () => {
    render(<PhpBenchResultSection
      section={{ id: 'phpbench-results', label: 'PHPBench 数据', view: 'phpbench-results', metrics: ['phpbench_score', 'phpbench_score_sample'] }}
      definitions={{ phpbench_score: { unit: 'Score', presentation: { userLabel: 'PHPBench 综合分数' } } }}
      evaluations={[{
        id: 'eval-phpbench', candidate: '测试机3', workload: 'phpbench', status: 'completed',
        parameters: { times_to_run: 3, test_timeout_minutes: 10 },
        metrics: [
          { name: 'phpbench_score', value: 523642, unit: 'Score', sampleCount: 3, statistic: 'mean' },
          { name: 'sample_count', value: 3, unit: 'count' },
          { name: 'pts_run_ok', value: true, unit: 'flag' },
          { name: 'profile_version_match', value: true, unit: 'flag' },
        ],
        artifacts: [{ name: 'pts-result.json', url: '/result' }, { name: 'run-metadata.json', url: '/metadata' }],
      }]}
    />);

    expect(screen.getByRole('heading', { name: 'PHPBench 数据' })).toBeInTheDocument();
    expect(screen.getByText('523,642 Score')).toBeInTheDocument();
    expect(screen.getByText('PHPBench 综合分数（平均值）')).toBeInTheDocument();
    expect(screen.getByText('3 次成功采集')).toBeInTheDocument();
    expect(screen.queryByText('517,432')).not.toBeInTheDocument();
    expect(screen.getByText('每轮重复').nextSibling).toHaveTextContent('3 次');
    expect(screen.getByText('单项超时').nextSibling).toHaveTextContent('10 分钟');
    expect(screen.getByText('PTS 执行通过')).toBeInTheDocument();
    expect(screen.getByText('PTS 原始结果已回传')).toBeInTheDocument();
  });

  it('renders validity gates grouped by measured round and keeps details collapsed', () => {
    render(<ValidityGatesSection
      section={{ id: 'validity-gates', label: '有效性门禁', metrics: ['failed_request_ratio'] }}
      evaluations={[{
        id: 'eval-dcperf', candidate: '测试机3', status: 'completed',
        runs: [{
          attemptId: 'att-1', round: 1, measured: true, status: 'completed',
          metrics: [{ name: 'closed_loop_successful_rps', value: 155, unit: 'requests/second' }],
          gateResults: [{ id: 'timeout-budget', passed: true, message: 'timeout request ratio is within the closed-loop budget', details: { timeoutRatio: 0 } }],
        }],
      }]}
    />);

    expect(screen.getByText('第 1 轮')).toBeInTheDocument();
    expect(screen.getByText('1 / 1 通过')).toBeInTheDocument();
    expect(screen.getByText('超时预算门禁')).toBeInTheDocument();
    expect(screen.getByText('超时请求率处于闭环预算范围内。')).toBeInTheDocument();
    expect(screen.getByText('timeout-budget')).toBeInTheDocument();
    expect(screen.getByText('超时率: 0')).toBeInTheDocument();
  });

  it('renders only the complete raw evidence download', () => {
    render(<Evidence
      items={[{
        id: 'evidence-summary', title: 'Immutable experiment evidence', kind: 'content-addressed',
        summary: '5 attempts, 33 observations, 68 artifacts',
        artifacts: [{ name: '完整证据包', url: '/evidence' }],
      }]}
    />);

    expect(screen.getByRole('heading', { name: '完整原始数据' })).toBeInTheDocument();
    expect(screen.getByText('5 次尝试 · 33 条观测 · 68 个制品')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /完整证据包/ })).toBeInTheDocument();
    expect(screen.queryByText(/第 1 轮/)).not.toBeInTheDocument();
    expect(screen.queryByText(/尝试 ID/)).not.toBeInTheDocument();
  });

  it('renders contract-backed optimization advice without a ranking conclusion', () => {
    render(<VariabilityPanel data={{
      experiment_id: 'exp-1', mode: 'selection', metric: 'workload-specific', unit: 'varies', direction: 'varies',
      status: 'available', groups: [{
        groupLabel: '测试机3 · memory', targetId: 'target-3', workloadId: 'memory', metric: 'throughput_mib_s',
        unit: 'MiB/s', direction: 'maximize', status: 'stable', invalidAttemptCount: 1,
        distribution: { count: 5, mean: 4467.92, median: 4460, standardDeviation: 20, coefficientOfVariation: 0.004, minimum: 4430, maximum: 4500 },
        stability: { verdict: 'stable', reasons: ['CV=0.004，原始结果稳定'] }, modes: null, runs: [], outliers: { slow: [], fast: [] },
        associationClues: [], attribution: [], selectionImpact: { summary: '', confidence: 'high' }, evidence: { sampleCount: 5 },
        recommendations: [{ ruleId: 'sysbench.stable', source: 'benchmark-contract', action: '保持配置复测', rationale: '合同稳定阈值已满足', priority: 'low', kind: 'diagnostic' }],
      }], comparisons: [], studyRecommendations: [{ ruleId: 'sysbench.single-target', source: 'benchmark-contract', action: '增加目标机复测', rationale: '单目标不形成采购结论', priority: 'medium', kind: 'retest' }],
      diagnosticContractDigest: 'sha256:contract',
    }} />);

    expect(screen.getByRole('heading', { name: '优化建议' })).toBeInTheDocument();
    expect(screen.getByText('内存吞吐')).toBeInTheDocument();
    expect(screen.getByText('4,467.92 MiB/s')).toBeInTheDocument();
    expect(screen.getByText(/不形成性能优劣、领先或采购结论/)).toBeInTheDocument();
    expect(screen.getByText('保持配置复测')).toBeInTheDocument();
    expect(screen.queryByText('单位价格容量')).not.toBeInTheDocument();
  });
});
