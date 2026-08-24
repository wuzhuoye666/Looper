import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { experimentTabs, PhpBenchResultSection, SysbenchWorkloadSection } from '../pages/ExperimentDetailPage';
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
          { name: 'events_per_sec', value: 4576065.04, unit: 'events/s' },
          { name: 'throughput_mib_s', value: 4467.92, unit: 'MiB/s' },
        ],
      }]}
    />);

    expect(screen.getByRole('heading', { name: 'Sysbench workload 数据' })).toBeInTheDocument();
    expect(screen.getByText('内存吞吐')).toBeInTheDocument();
    expect(screen.getByText('4,467.92 MiB/s')).toBeInTheDocument();
    expect(screen.getByText('已回传指标与原始证据')).toBeInTheDocument();
  });

  it('renders PHPBench score, samples, parameters, gates, and evidence in its dedicated panel', () => {
    render(<PhpBenchResultSection
      section={{ id: 'phpbench-results', label: 'PHPBench 数据', view: 'phpbench-results', metrics: ['phpbench_score', 'phpbench_score_sample'] }}
      definitions={{ phpbench_score: { unit: 'Score', presentation: { userLabel: 'PHPBench 综合分数' } } }}
      evaluations={[{
        id: 'eval-phpbench', candidate: '测试机3', workload: 'phpbench', status: 'completed',
        parameters: { times_to_run: 3, test_timeout_minutes: 10 },
        metrics: [
          { name: 'phpbench_score', value: 523642, unit: 'Score' },
          { name: 'phpbench_score_sample', value: 517432, unit: 'Score', sampleIndex: 0 },
          { name: 'phpbench_score_sample', value: 528171, unit: 'Score', sampleIndex: 1 },
          { name: 'phpbench_score_sample', value: 525322, unit: 'Score', sampleIndex: 2 },
          { name: 'sample_count', value: 3, unit: 'count' },
          { name: 'pts_run_ok', value: true, unit: 'flag' },
          { name: 'profile_version_match', value: true, unit: 'flag' },
        ],
        artifacts: [{ name: 'pts-result.json', url: '/result' }, { name: 'run-metadata.json', url: '/metadata' }],
      }]}
    />);

    expect(screen.getByRole('heading', { name: 'PHPBench 数据' })).toBeInTheDocument();
    expect(screen.getByText('523,642 Score')).toBeInTheDocument();
    expect(screen.getByText('517,432')).toBeInTheDocument();
    expect(screen.getByText('每轮重复').nextSibling).toHaveTextContent('3 次');
    expect(screen.getByText('单项超时').nextSibling).toHaveTextContent('10 分钟');
    expect(screen.getByText('PTS 执行通过')).toBeInTheDocument();
    expect(screen.getByText('PTS 原始结果已回传')).toBeInTheDocument();
  });
});
