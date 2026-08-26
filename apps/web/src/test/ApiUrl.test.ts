import { afterEach, describe, expect, it, vi } from 'vitest';
import { resolveApiUrl } from '../lib/api';

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe('API base configuration', () => {
  it('sends production requests to the current website by default', async () => {
    vi.stubEnv('PROD', true);
    vi.stubEnv('VITE_API_BASE_URL', '');
    vi.resetModules();
    const { api, resolveApiUrl: resolveConfiguredApiUrl } = await import('../lib/api');
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);

    await api.dashboard();

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/dashboard', expect.any(Object));
    expect(resolveConfiguredApiUrl(undefined, 'http://8.163.35.64').host).toBe('8.163.35.64');
  });

  it('keeps the loopback API default for local development', async () => {
    vi.stubEnv('PROD', false);
    vi.stubEnv('VITE_API_BASE_URL', '');
    vi.resetModules();
    const { API_BASE } = await import('../lib/api');

    expect(API_BASE).toBe('http://127.0.0.1:8000/api/v1');
  });

  it('honors an explicit API URL in production', async () => {
    vi.stubEnv('PROD', true);
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.com/api/v1/');
    vi.resetModules();
    const { API_BASE } = await import('../lib/api');

    expect(API_BASE).toBe('https://api.example.com/api/v1');
  });
});

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
