import { describe, expect, it } from 'vitest';
import { eventToLines } from '../components/ExperimentTerminal';

describe('ExperimentTerminal raw output', () => {
  it('renders every physical stdout line without rewriting its content', () => {
    const text = 'line 1\n  raw spacing\t\n';
    const lines = eventToLines({
      sequence: 7,
      type: 'attempt.log',
      entityType: 'attempt',
      entityId: 'att-1',
      payload: { stream: 'stdout', stage: 'run', text },
      createdAt: '2026-08-24T00:00:00Z',
    });
    expect(lines.map(line => line.text).join('')).toBe(text);
    expect(lines.map(line => line.text)).toEqual(['line 1\n', '  raw spacing\t\n']);
    expect(lines[0]).toMatchObject({ sequence: 7, attemptId: 'att-1', stream: 'stdout' });
  });

  it('includes process lifecycle system lines', () => {
    expect(eventToLines({
      sequence: 9,
      type: 'attempt.log',
      entityType: 'attempt',
      entityId: 'att-2',
      payload: {
        stream: 'system', stage: 'run', text: 'process exited code=0\n',
        workerId: 'remote-1', fencingToken: 2,
      },
      createdAt: '2026-08-24T00:00:00Z',
    })[0]).toMatchObject({ stream: 'system', workerId: 'remote-1', fencingToken: 2 });
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
