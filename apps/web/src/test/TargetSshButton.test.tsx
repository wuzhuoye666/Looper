import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { TargetSshButton } from '../components/TargetSshButton';

afterEach(() => cleanup());

it('复用服务端保存的 SSH 凭据并恢复 Worker', async () => {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => new Response(JSON.stringify({
    id: 'external:10.0.0.8',
    name: 'compute-01',
    credentialsRemembered: true,
    connectionTest: { status: 'connected', testedAt: '2026-08-23T00:00:00Z' },
    deployment: { status: 'deploying', workerId: 'remote-test' },
  }), { status: init?.method === 'POST' ? 200 : 200, headers: { 'Content-Type': 'application/json' } }));
  vi.stubGlobal('fetch', fetchMock);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><TargetSshButton target={{
    id: 'external:10.0.0.8', name: 'compute-01', runnable: true,
    lifecycleStatus: 'active', credentialsRemembered: true,
  }}/></QueryClientProvider>);

  fireEvent.click(screen.getByRole('button', { name: 'compute-01 · 测试 SSH' }));

  expect(await screen.findByRole('button', { name: 'compute-01 · SSH 已连通' })).toBeInTheDocument();
  expect(screen.getByText('凭据已复用，Worker 正在自动上线')).toBeInTheDocument();
  expect(screen.queryByRole('link', { name: '最终测试' })).not.toBeInTheDocument();
  const [url, init] = fetchMock.mock.calls[0];
  expect(String(url)).toContain('/targets/external%3A10.0.0.8/ssh-test');
  expect(init?.method).toBe('POST');
  expect(init?.body).toBeUndefined();
});

it('没有保存凭据时明确提示首次连接', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><TargetSshButton target={{
    id: 'external:10.0.0.9', name: 'compute-02', credentialsRemembered: false,
  }}/></QueryClientProvider>);

  expect(screen.getByText('未保存 SSH 凭据')).toBeInTheDocument();
  expect(screen.queryByRole('button')).not.toBeInTheDocument();
});
