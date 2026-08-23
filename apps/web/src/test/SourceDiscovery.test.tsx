import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../App';

afterEach(() => vi.restoreAllMocks());

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/interfaces']}><App/></MemoryRouter></QueryClientProvider>);
}

it('明确展示 DeepSeek 数据流与非 ZIP 约束', async () => {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const body = url.endsWith('/readiness')
      ? { configured: false, provider: 'deepseek', model: 'deepseek-v4-flash', baseUrl: 'https://api.deepseek.com', maxArchiveBytes: 20971520, acceptedMediaTypes: ['application/zip'], requiredEnvironment: ['LOOPER_DEEPSEEK_API_KEY'], dataDisclosure: 'source snippets sent' }
      : { items: [], total: 0 };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
  renderPage();
  expect(await screen.findByRole('heading', { name: '动态接口发现' })).toBeInTheDocument();
  expect(await screen.findByText('DeepSeek Harness 尚未配置')).toBeInTheDocument();
  expect(screen.getByText('只读 Harness')).toBeInTheDocument();
  const input = screen.getByLabelText('源码 ZIP');
  fireEvent.change(input, { target: { files: [new File(['x'], 'app.py', { type: 'text/x-python' })] } });
  expect(await screen.findByText(/只接受 .zip 源码包/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '开始动态发现' })).toBeDisabled();
});
