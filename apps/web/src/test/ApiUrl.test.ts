import { describe, expect, it } from 'vitest';
import { resolveApiUrl } from '../lib/api';

describe('resolveApiUrl', () => {
  it('resolves a same-origin production API path', () => {
    const result = resolveApiUrl('/api/v1', 'http://8.163.35.64');

    expect(result.href).toBe('http://8.163.35.64/api/v1');
    expect(result.host).toBe('8.163.35.64');
  });

  it('preserves an absolute development API URL', () => {
    const result = resolveApiUrl('http://127.0.0.1:8000/api/v1', 'http://localhost:5173');

    expect(result.origin).toBe('http://127.0.0.1:8000');
  });
});
