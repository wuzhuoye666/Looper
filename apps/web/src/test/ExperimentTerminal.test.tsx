import { describe, expect, it } from 'vitest';
import { eventToLines } from '../components/ExperimentTerminal';

describe('ExperimentTerminal raw output', () => {
  it('preserves command stdout and stderr text exactly', () => {
    const text = 'line 1\n  raw spacing\t\n';
    expect(eventToLines({
      sequence: 7,
      type: 'attempt.log',
      entityType: 'attempt',
      entityId: 'att-1',
      payload: { stream: 'stdout', stage: 'run', text },
      createdAt: '2026-08-24T00:00:00Z',
    })[0].text).toBe(text);
  });

  it('does not mix curated phase summaries into the raw terminal', () => {
    expect(eventToLines({
      sequence: 8,
      type: 'attempt.phase.changed',
      entityType: 'attempt',
      entityId: 'att-1',
      payload: { phase: 'running-benchmark', detail: '正在执行 Benchmark' },
      createdAt: '2026-08-24T00:00:00Z',
    })).toEqual([]);
  });
});
