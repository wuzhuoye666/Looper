import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ImportTargetDialog } from '../components/ImportTargetDialog';

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>
    <ImportTargetDialog open onClose={() => undefined} />
  </QueryClientProvider>);
}

describe('外部机器 SSH 自动发现', () => {
  afterEach(() => cleanup());

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const data = url.endsWith('/targets/connect') ? {
        id: 'external:10.0.0.8', name: 'compute-01', endpoint: '10.0.0.8', status: 'unknown',
        framework: 'Ubuntu 24.04.2 LTS', version: 'Linux 6.8.0-60-generic', runnable: false,
        credentialsRemembered: true,
        fingerprint: {
          processor: 'AMD EPYC 7B13', logical_cpu_count: 16, memory_gib: 62.78,
          architecture: 'x86_64', host_key_sha256: `SHA256:${'A'.repeat(43)}`,
        },
      } : { items: [] };
      return new Response(JSON.stringify(data), {
        status: init?.method === 'POST' ? 201 : 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }));
  });

  it('只提交连接信息并展示服务端发现的机器参数', async () => {
    renderDialog();

    fireEvent.change(screen.getByLabelText('IP / 主机名 *'), { target: { value: '10.0.0.8' } });
    fireEvent.change(screen.getByLabelText('用户名 *'), { target: { value: 'ubuntu' } });
    fireEvent.change(screen.getByLabelText('连接方式 *'), { target: { value: 'password' } });
    fireEvent.change(screen.getByLabelText('SSH 密码 *'), { target: { value: 'one-time-secret' } });
    fireEvent.click(screen.getByRole('button', { name: '连接并部署' }));

    expect(await screen.findByText('连接成功，Worker 已部署')).toBeInTheDocument();
    expect(screen.getByText('compute-01')).toBeInTheDocument();
    expect(screen.getByText('AMD EPYC 7B13')).toBeInTheDocument();
    expect(screen.getByText('62.78 GiB')).toBeInTheDocument();
    expect(screen.getByText('x86_64')).toBeInTheDocument();
    expect(screen.getByText(/后端重启时会校验主机指纹并自动重建隧道/)).toBeInTheDocument();

    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/targets/connect'));
      const body = JSON.parse(String(call?.[1]?.body));
      expect(body).toMatchObject({
        auth_method: 'password', password: 'one-time-secret',
      });
      expect(body).not.toHaveProperty('hardware');
      expect(body).not.toHaveProperty('name');
    });
  });

  it('默认使用 root:22 并通过文件导入私钥，可关闭凭据保存', async () => {
    renderDialog();

    const keyFile = new File(['-----BEGIN OPENSSH PRIVATE KEY-----\nTEST\n-----END OPENSSH PRIVATE KEY-----'], 'Looper.pem', { type: 'application/x-pem-file' });
    fireEvent.change(screen.getByLabelText('SSH 私钥文件 *'), { target: { files: [keyFile] } });
    await waitFor(() => expect(screen.getByText('Looper.pem')).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole('button', { name: '连接并部署' })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('checkbox', {
      name: accessibleName => accessibleName.startsWith('保存密钥 / 密码'),
    }));
    fireEvent.change(screen.getByLabelText('IP / 主机名 *'), { target: { value: '10.0.0.9' } });
    fireEvent.submit(screen.getByRole('dialog'));

    await screen.findByText('连接成功，Worker 已部署');
    await waitFor(() => {
      const call = vi.mocked(fetch).mock.calls.find(([input]) => String(input).endsWith('/targets/connect'));
      const body = JSON.parse(String(call?.[1]?.body));
      expect(body).toMatchObject({ endpoint: '10.0.0.9', port: 22, username: 'root', auth_method: 'private-key', private_key: expect.stringContaining('BEGIN OPENSSH PRIVATE KEY'), remember_credentials: false });
    });
  });
});
