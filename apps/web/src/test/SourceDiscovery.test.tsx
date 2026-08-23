import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, expect, it, vi } from 'vitest';
import { App } from '../App';
import { OPERATOR_ACCESS_CHANGED_EVENT } from '../components/OperatorAccess';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/interfaces']}><App/></MemoryRouter></QueryClientProvider>);
}

function mockApi(configured: boolean) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const body = url.endsWith('/readiness')
      ? { configured, provider: 'deepseek', model: 'deepseek-v4-flash', baseUrl: 'https://api.deepseek.com', maxArchiveBytes: 20971520, acceptedMediaTypes: ['application/zip'], requiredEnvironment: configured ? [] : ['LOOPER_DEEPSEEK_API_KEY'], dataDisclosure: 'source snippets sent' }
      : url.endsWith('/provider-config')
        ? { configured: configured || init?.method === 'PUT', source: init?.method === 'PUT' ? 'stored' : configured ? 'environment' : null, maskedKey: configured || init?.method === 'PUT' ? '••••••••1234' : null, provider: 'deepseek', model: 'deepseek-v4-flash', baseUrl: 'https://api.deepseek.com', encryptedAtRest: init?.method === 'PUT' }
      : { items: [], total: 0 };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  }));
}

it('未配置 DeepSeek 时锁定上传并说明配置约束', async () => {
  mockApi(false);
  renderPage();
  expect(await screen.findByRole('heading', { name: '动态接口发现' })).toBeInTheDocument();
  expect(await screen.findByText('DeepSeek Harness 尚未配置')).toBeInTheDocument();
  expect(screen.getByText('只读 Harness')).toBeInTheDocument();
  expect(screen.getByLabelText('源码 ZIP')).toBeDisabled();
  expect(screen.getByRole('button', { name: '配置 DeepSeek 后可开始' })).toBeDisabled();
});

it('已配置时在前端拒绝非 ZIP 文件', async () => {
  mockApi(true);
  renderPage();
  await screen.findByText('deepseek-v4-flash 已就绪');
  const input = screen.getByLabelText('源码 ZIP');
  fireEvent.change(input, { target: { files: [new File(['x'], 'app.py', { type: 'text/x-python' })] } });
  expect(await screen.findByText(/只接受 .zip 源码包/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '开始动态发现' })).toBeDisabled();
});

it('操作员认证变化后自动重新加载发现记录', async () => {
  mockApi(true);
  renderPage();
  await screen.findByText('deepseek-v4-flash 已就绪');
  const callsBefore = vi.mocked(fetch).mock.calls.filter(([input]) => String(input).endsWith('/source-discoveries')).length;
  window.dispatchEvent(new Event(OPERATOR_ACCESS_CHANGED_EVENT));
  await vi.waitFor(() => {
    const callsAfter = vi.mocked(fetch).mock.calls.filter(([input]) => String(input).endsWith('/source-discoveries')).length;
    expect(callsAfter).toBeGreaterThan(callsBefore);
  });
});

it('通过后端保存 DeepSeek Key 并立即清除前端明文', async () => {
  mockApi(false);
  renderPage();
  const input = await screen.findByLabelText('新的 DeepSeek API Key');
  const secret = 'sk-browser-only-secret-value-1234';
  fireEvent.change(input, { target: { value: secret } });
  fireEvent.click(screen.getByRole('button', { name: '加密保存' }));
  expect(await screen.findByText(/浏览器中的输入已清除/)).toBeInTheDocument();
  expect(input).toHaveValue('');
  expect(window.sessionStorage.getItem('looper.deepseek-key')).toBeNull();
  const request = vi.mocked(fetch).mock.calls.find(([url, init]) => String(url).endsWith('/provider-config') && init?.method === 'PUT');
  expect(JSON.parse(String(request?.[1]?.body))).toEqual({ apiKey: secret });
});
