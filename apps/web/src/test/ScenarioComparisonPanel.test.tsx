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

const vgoComparison: ScenarioComparison = {
  id: 'vgo@1.1.2',
  scenarioId: 'stability.vgo.cpu-variability',
  scenarioName: 'VGO 稳定性对比',
  benchmarkId: 'looper.vgo.variability',
  benchmarkName: 'VGO 性能波动与稳定性测试',
  benchmarkVersion: '1.1.2',
  axes: [
    { key: 'matmul:runtime_cv', workloadId: 'matmul', workloadLabel: 'Matmul 内存分配波动', label: '基线 CV', metric: 'runtime_cv', unit: 'ratio', direction: 'minimize' },
    { key: 'matmul:optimized_runtime_cv', workloadId: 'matmul', workloadLabel: 'Matmul 内存分配波动', label: '优化后 CV', metric: 'optimized_runtime_cv', unit: 'ratio', direction: 'minimize' },
    { key: 'matmul:optimized_median_runtime_seconds', workloadId: 'matmul', workloadLabel: 'Matmul 内存分配波动', label: '优化后中位耗时', metric: 'optimized_median_runtime_seconds', unit: 's', direction: 'minimize' },
    { key: 'matmul:optimized_p95_runtime_seconds', workloadId: 'matmul', workloadLabel: 'Matmul 内存分配波动', label: '优化后 P95', metric: 'optimized_p95_runtime_seconds', unit: 's', direction: 'minimize' },
    { key: '7z:runtime_cv', workloadId: '7z', workloadLabel: '7-Zip 单线程波动', label: '基线 CV', metric: 'runtime_cv', unit: 'ratio', direction: 'minimize' },
    { key: '7z:optimized_runtime_cv', workloadId: '7z', workloadLabel: '7-Zip 单线程波动', label: '优化后 CV', metric: 'optimized_runtime_cv', unit: 'ratio', direction: 'minimize' },
    { key: '7z:optimized_median_runtime_seconds', workloadId: '7z', workloadLabel: '7-Zip 单线程波动', label: '优化后中位耗时', metric: 'optimized_median_runtime_seconds', unit: 's', direction: 'minimize' },
    { key: '7z:optimized_p95_runtime_seconds', workloadId: '7z', workloadLabel: '7-Zip 单线程波动', label: '优化后 P95', metric: 'optimized_p95_runtime_seconds', unit: 's', direction: 'minimize' },
  ],
  targets: [
    { targetId: 'vgo-a', label: '机器 A', studyCount: 1, validSampleCount: 16, values: {
      'matmul:runtime_cv': { raw: 0.04, normalized: 50, studyCount: 1, sampleCount: 1 },
      'matmul:optimized_runtime_cv': { raw: 0.02, normalized: 75, studyCount: 1, sampleCount: 1 },
      'matmul:optimized_median_runtime_seconds': { raw: 1.2, normalized: 100, studyCount: 1, sampleCount: 1 },
      'matmul:optimized_p95_runtime_seconds': { raw: 1.5, normalized: 100, studyCount: 1, sampleCount: 1 },
      '7z:runtime_cv': { raw: 0.03, normalized: 100, studyCount: 1, sampleCount: 1 },
      '7z:optimized_runtime_cv': { raw: 0.02, normalized: 100, studyCount: 1, sampleCount: 1 },
      '7z:optimized_median_runtime_seconds': { raw: 2.1, normalized: 90, studyCount: 1, sampleCount: 1 },
      '7z:optimized_p95_runtime_seconds': { raw: 2.6, normalized: 90, studyCount: 1, sampleCount: 1 },
    } },
    { targetId: 'vgo-b', label: '机器 B', studyCount: 1, validSampleCount: 16, values: {
      'matmul:runtime_cv': { raw: 0.02, normalized: 100, studyCount: 1, sampleCount: 1 },
      'matmul:optimized_runtime_cv': { raw: 0.015, normalized: 100, studyCount: 1, sampleCount: 1 },
      'matmul:optimized_median_runtime_seconds': { raw: 1.5, normalized: 80, studyCount: 1, sampleCount: 1 },
      'matmul:optimized_p95_runtime_seconds': { raw: 1.8, normalized: 83.3, studyCount: 1, sampleCount: 1 },
      '7z:runtime_cv': { raw: 0.04, normalized: 75, studyCount: 1, sampleCount: 1 },
      '7z:optimized_runtime_cv': { raw: 0.025, normalized: 80, studyCount: 1, sampleCount: 1 },
      '7z:optimized_median_runtime_seconds': { raw: 1.9, normalized: 100, studyCount: 1, sampleCount: 1 },
      '7z:optimized_p95_runtime_seconds': { raw: 2.3, normalized: 100, studyCount: 1, sampleCount: 1 },
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

  it('VGO 每次只展示一个负载的四项关键结果', () => {
    render(<ScenarioComparisonPanel comparisons={[vgoComparison]} />);
    const workload = screen.getByLabelText('选择 VGO 测试负载');
    expect(workload).toHaveValue('matmul');
    expect(screen.getByRole('img', { name: /Matmul 内存分配波动目标机能力对比.*优化后 P95/ }))
      .toHaveAttribute('data-chart-kind', 'radar');
    expect(screen.getAllByText('基线 CV').length).toBeGreaterThan(0);
    expect(screen.getAllByText('2%').length).toBeGreaterThan(0);
    fireEvent.change(workload, { target: { value: '7z' } });
    expect(screen.getByRole('img', { name: /7-Zip 单线程波动目标机能力对比/ })).toBeInTheDocument();
  });
});
