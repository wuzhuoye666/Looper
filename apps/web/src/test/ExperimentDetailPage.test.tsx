import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { experimentTabs, SysbenchWorkloadSection } from '../pages/ExperimentDetailPage';
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
});
