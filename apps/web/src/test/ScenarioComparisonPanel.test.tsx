import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ScenarioComparisonPanel } from '../components/ScenarioComparisonPanel';
import type { ScenarioComparison } from '../lib/types';

const comparison: ScenarioComparison = {
  id: 'sysbench@1.0.2',
  scenarioId: 'sysbench',
  scenarioName: 'Sysbench 性能套件',
  benchmarkId: 'looper.sysbench',
  benchmarkName: 'Sysbench',
  benchmarkVersion: '1.0.2',
  updatedAt: '2026-08-25T10:00:00Z',
  axes: [
    { key: 'cpu', workloadId: 'cpu', label: 'CPU', metric: 'events_per_sec', unit: 'events/s', direction: 'maximize' },
    { key: 'memory', workloadId: 'memory', label: '内存', metric: 'throughput_mib_s', unit: 'MiB/s', direction: 'maximize' },
    { key: 'thread', workloadId: 'thread', label: '线程', metric: 'events_per_sec', unit: 'events/s', direction: 'maximize' },
    { key: 'mutex', workloadId: 'mutex', label: '互斥锁', metric: 'events_per_sec', unit: 'events/s', direction: 'maximize' },
  ],
  targets: [
    { targetId: 'a', label: '机器 A', studyCount: 2, validSampleCount: 8, values: {
      cpu: { raw: 200, normalized: 100, studyCount: 2, sampleCount: 2 },
      memory: { raw: 100, normalized: 50, studyCount: 2, sampleCount: 2 },
      thread: { raw: 160, normalized: 80, studyCount: 2, sampleCount: 2 },
      mutex: { raw: 80, normalized: 80, studyCount: 2, sampleCount: 2 },
    } },
    { targetId: 'b', label: '机器 B', studyCount: 1, validSampleCount: 4, values: {
      cpu: { raw: 100, normalized: 50, studyCount: 1, sampleCount: 1 },
      memory: { raw: 200, normalized: 100, studyCount: 1, sampleCount: 1 },
      thread: { raw: 200, normalized: 100, studyCount: 1, sampleCount: 1 },
      mutex: { raw: 100, normalized: 100, studyCount: 1, sampleCount: 1 },
    } },
    { targetId: 'c', label: '机器 C', studyCount: 1, validSampleCount: 4, values: {
      cpu: { raw: 150, normalized: 75, studyCount: 1, sampleCount: 1 },
      memory: { raw: 150, normalized: 75, studyCount: 1, sampleCount: 1 },
      thread: { raw: 120, normalized: 60, studyCount: 1, sampleCount: 1 },
      mutex: { raw: 60, normalized: 60, studyCount: 1, sampleCount: 1 },
    } },
    { targetId: 'd', label: '机器 D', studyCount: 1, validSampleCount: 4, values: {
      cpu: { raw: 180, normalized: 90, studyCount: 1, sampleCount: 1 },
      memory: { raw: 180, normalized: 90, studyCount: 1, sampleCount: 1 },
      thread: { raw: 180, normalized: 90, studyCount: 1, sampleCount: 1 },
      mutex: { raw: 90, normalized: 90, studyCount: 1, sampleCount: 1 },
    } },
  ],
};

describe('ScenarioComparisonPanel', () => {
  it('默认展示最新场景、雷达图和关键差异', () => {
    render(<ScenarioComparisonPanel comparisons={[comparison]} />);
    expect(screen.getByLabelText('选择对比场景')).toHaveValue(comparison.id);
    expect(screen.getByRole('img', { name: /Sysbench 性能套件目标机能力对比/ }))
      .toHaveAttribute('data-chart-kind', 'radar');
    expect(screen.getByText('关键差异')).toBeInTheDocument();
    expect(screen.getAllByText('领先 33.3%').length).toBeGreaterThan(0);
  });

  it('最多选择三台目标机，取消后可以替换', () => {
    render(<ScenarioComparisonPanel comparisons={[comparison]} />);
    const fourth = screen.getByRole('button', { name: /机器 D/ });
    expect(fourth).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: /机器 C/ }));
    expect(fourth).toBeEnabled();
    fireEvent.click(fourth);
    expect(fourth).toHaveAttribute('aria-pressed', 'true');
  });

  it('一到两个维度时降级为条形图', () => {
    const compact = { ...comparison, id: 'compact', axes: comparison.axes.slice(0, 2) };
    render(<ScenarioComparisonPanel comparisons={[compact]} />);
    expect(screen.getByRole('img')).toHaveAttribute('data-chart-kind', 'bar');
  });

  it('没有可比较数据时显示明确空状态', () => {
    render(<ScenarioComparisonPanel comparisons={[]} />);
    expect(screen.getByText('至少需要同一场景下两台目标机的有效结果。')).toBeInTheDocument();
  });
});
