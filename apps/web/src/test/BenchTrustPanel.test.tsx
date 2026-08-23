import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { BenchTrustPanel } from '../components/BenchTrustPanel';
import type { BenchTrustData } from '../lib/types';

function fixture(): BenchTrustData {
  return {
    schemaVersion: 'v1alpha1',
    methodVersion: '1.0.0',
    status: 'partial',
    referenceValidityRate: {
      status: 'insufficient_evidence',
      method: 'proportion of eligible target environments',
      valid_environment_count: 0,
      eligible_environment_count: 1,
      excluded_environment_count: 0,
      rate: null,
      confidence_interval: null,
      expected_direction: 'maximize',
      minimum_effect: 0.05,
      environment_results: [],
      criteria: [],
      limitations: [],
    },
    rankStability: {
      status: 'insufficient_evidence',
      axes: [{
        axis: 'machine', scoring_formula_ids: null, slice_count: 1, candidate_count: 3,
        comparison_count: 0, method: 'kendall_tau_b', median_tau: null,
        minimum_tau: null, maximum_tau: null, pairwise_flip_rate: null, tie_count: 0,
        limitations: [],
      }],
      limitations: [],
    },
    taskLeverage: {
      status: 'available', scoring_formula: 'weighted-sum', aggregation_method: 'weighted-sum',
      maximum_contribution_share: 0.5, dominant_task: 't1',
      top_contributors: [{ task_id: 't1', weight: 1, contribution: 100, contribution_share: 0.5 }],
      leave_one_out: { maximum_rank_shift: 1, winner_changed: false, task_shifts: {} },
      limitations: [],
    },
    environmentSensitivity: {
      status: 'insufficient_evidence', method: 'controlled eta-squared', analysis_unit: 'residual',
      sample_count: 1, controls: [], total_explained_ratio: null, factors: [], residual_ratio: null,
      warnings: ['样本数 1 低于下限 5'], limitations: [], association_only: true,
    },
    evidence: { sample_count: 0, target_count: 1, distinct_dates: 1, distinct_workloads: 1 },
    limitations: ['BenchTrust 元指标是证据，不作为硬门禁'],
    inputDigest: 'sha256:aa', policyDigest: 'sha256:bb',
  };
}

describe('BenchTrustPanel', () => {
  it('渲染四张中性结果卡，不产生综合可信度分数', () => {
    render(<BenchTrustPanel data={fixture()} />);
    expect(screen.getByText('Reference Validity Rate')).toBeInTheDocument();
    expect(screen.getByText('Rank Stability')).toBeInTheDocument();
    expect(screen.getByText('Task Leverage')).toBeInTheDocument();
    expect(screen.getByText('Environment Sensitivity')).toBeInTheDocument();
    expect(screen.queryByText('综合可信度')).not.toBeInTheDocument();
    expect(screen.queryByText(/可信度评分/)).not.toBeInTheDocument();
  });

  it('标注统计关联并正确把 null 显示为占位符而非 0', () => {
    render(<BenchTrustPanel data={fixture()} />);
    expect(screen.getAllByText(/统计关联，不代表因果关系/)).toHaveLength(2);
    expect(screen.queryByText('0%')).not.toBeInTheDocument();
    expect(screen.getAllByText('—').length).toBeGreaterThan(0);
  });

  it('四项指标数据不足时分别说明状态', () => {
    render(<BenchTrustPanel data={fixture()} />);
    expect(screen.getAllByText('证据不足').length).toBeGreaterThanOrEqual(2);
  });
});
