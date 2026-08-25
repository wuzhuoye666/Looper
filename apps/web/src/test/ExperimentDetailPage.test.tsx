import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
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
      '概览', '目标结果', '吞吐与延迟', '有效性门禁', '证据', '配置', '原始终端',
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

  it('renders localized evidence in full-width file sections and distinguishes retries', () => {
    render(<Evidence
      items={[{
        id: 'evidence-summary', title: 'Immutable experiment evidence', kind: 'content-addressed',
        summary: '5 attempts, 33 observations, 68 artifacts',
        artifacts: [{ name: '完整证据包', url: '/evidence' }],
      }]}
      evaluations={[{
        id: 'eval-dcperf', candidate: '测试机3', status: 'completed', runs: [
          {
            attemptId: 'att-first', round: 1, retry: 0, status: 'failed', measured: false,
            artifacts: [{ name: 'prepare.stdout.log', url: '/prepare' }], error: '缺少结果文件',
          },
          {
            attemptId: 'att-retry', round: 1, retry: 1, status: 'completed', measured: true,
            artifacts: [{ name: 'result.json', url: '/result' }],
          },
        ],
      }]}
    />);

    expect(screen.getByRole('heading', { name: '完整实验原始证据' })).toBeInTheDocument();
    expect(screen.getByText('5 次尝试 · 33 条观测 · 68 个制品')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '第 1 轮 · 首次尝试' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '第 1 轮 · 重试 1' })).toBeInTheDocument();
    expect(screen.getByText('环境准备标准输出')).toBeInTheDocument();
    expect(screen.getByText('标准化测试结果')).toBeInTheDocument();
    expect(screen.getAllByText('证据文件')).toHaveLength(3);
    const firstFiles = screen.getAllByText('证据文件')[0].closest('details');
    expect(firstFiles).not.toHaveAttribute('open');
    fireEvent.click(screen.getAllByText('证据文件')[0]);
    expect(firstFiles).toHaveAttribute('open');
  });
});
