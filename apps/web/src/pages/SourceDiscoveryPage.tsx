import { AlertTriangle, Braces, CheckCircle2, Download, FileArchive, LoaderCircle, RefreshCw, ShieldCheck, Upload } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { PageHeader } from '../components/PageHeader';
import { OPERATOR_ACCESS_CHANGED_EVENT } from '../components/OperatorAccess';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';
import type { SourceDiscovery, SourceDiscoveryReadiness } from '../lib/types';

function bytes(value: number) { return value >= 1024 * 1024 ? `${(value / 1024 / 1024).toFixed(0)} MiB` : `${Math.ceil(value / 1024)} KiB`; }
function downloadContract(item: SourceDiscovery) {
  if (!item.contract) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(item.contract, null, 2)], { type: 'application/json' }));
  const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${item.id}.interface-contract.json`; anchor.click(); URL.revokeObjectURL(url);
}

export function SourceDiscoveryPage() {
  const [readiness, setReadiness] = useState<SourceDiscoveryReadiness | null>(null);
  const [items, setItems] = useState<SourceDiscovery[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>();
  const [file, setFile] = useState<File | null>(null);
  const [consent, setConsent] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState('');
  const input = useRef<HTMLInputElement>(null);

  async function load() {
    setLoading(true); setError(undefined);
    try { const [ready, history] = await Promise.all([api.sourceDiscoveryReadiness(), api.sourceDiscoveries()]); setReadiness(ready); setItems(history.items); }
    catch (reason) { setError(reason); }
    finally { setLoading(false); }
  }
  useEffect(() => {
    void load();
    const reload = () => { void load(); };
    window.addEventListener(OPERATOR_ACCESS_CHANGED_EVENT, reload);
    return () => window.removeEventListener(OPERATOR_ACCESS_CHANGED_EVENT, reload);
  }, []);

  function choose(next: File | null) {
    setSubmitError('');
    if (!next) { setFile(null); return; }
    if (!next.name.toLowerCase().endsWith('.zip')) { setFile(null); setSubmitError('只接受 .zip 源码包；不会静态解析 OpenAPI、单个源码文件或其他压缩格式。'); return; }
    if (readiness && next.size > readiness.maxArchiveBytes) { setFile(null); setSubmitError(`源码包超过 ${bytes(readiness.maxArchiveBytes)} 限制。`); return; }
    setFile(next);
  }
  async function submit() {
    if (!file || !consent || !readiness?.configured) return;
    setSubmitting(true); setSubmitError('');
    try { const created = await api.discoverSource(file); setItems(current => [created, ...current.filter(item => item.id !== created.id)]); setFile(null); setConsent(false); if (input.current) input.current.value = ''; }
    catch (reason) { setSubmitError(reason instanceof Error ? reason.message : '动态接口发现失败'); await load(); }
    finally { setSubmitting(false); }
  }

  return <div className="page source-discovery-page">
    <PageHeader title="动态接口发现" description="DeepSeek Agent 通过只读文件工具理解源码，输出可追溯的统一接口合同；不执行用户代码，也不访问源码中的目标地址。" actions={<button className="button secondary" onClick={() => void load()} disabled={loading}><RefreshCw size={15}/>刷新</button>}/>
    {loading ? <LoadingState label="正在检查 DeepSeek 配置和发现历史"/> : error ? <ErrorState error={error} onRetry={() => void load()}/> : <>
      {!readiness?.configured && <section className="notice warning" role="alert"><AlertTriangle size={19}/><div><strong>DeepSeek Harness 尚未配置</strong><p>在 API 服务环境设置 <code>LOOPER_DEEPSEEK_API_KEY</code> 后重启。密钥仅保存在后端环境变量中，前端不接收或保存密钥。</p></div></section>}
      <section className="panel discovery-upload-panel">
        <div className="panel-heading"><div><h2>上传源码 ZIP</h2><p>限制 {bytes(readiness?.maxArchiveBytes || 0)} · 加密包、符号链接、越界路径会被拒绝 · 密钥文件和依赖目录会被排除</p></div><span className={`discovery-readiness ${readiness?.configured ? 'ready' : ''}`}>{readiness?.configured ? <CheckCircle2 size={14}/> : <AlertTriangle size={14}/>} {readiness?.configured ? `${readiness.model} 已就绪` : '未配置'}</span></div>
        <div className="discovery-upload-body">
          <div className="discovery-flow" aria-label="数据流"><span><FileArchive size={18}/><strong>源码 ZIP</strong><small>安全筛选</small></span><i>→</i><span><ShieldCheck size={18}/><strong>只读 Harness</strong><small>list / search / read</small></span><i>→</i><span><Braces size={18}/><strong>接口合同</strong><small>{'looper.dev/interface-contract/v1'}</small></span></div>
          <label className={`discovery-drop ${!readiness?.configured ? 'disabled' : ''}`} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); if (readiness?.configured) choose(event.dataTransfer.files[0] || null); }}>
            <Upload size={24}/><strong>{file ? file.name : '选择或拖入源码 ZIP'}</strong><small>{file ? bytes(file.size) : '仅 ZIP；源码只用于本次只读分析，不在服务器保存原包'}</small>
            <input ref={input} type="file" accept=".zip,application/zip" aria-label="源码 ZIP" disabled={!readiness?.configured} onChange={event => choose(event.target.files?.[0] || null)}/>
          </label>
          <label className={`discovery-consent ${!readiness?.configured ? 'disabled' : ''}`}><input type="checkbox" disabled={!readiness?.configured} checked={consent} onChange={event => setConsent(event.target.checked)}/><span>我确认该源码允许发送到配置的 DeepSeek 端点。Harness 仅会发送为接口判断所需的非敏感源码片段。</span></label>
          {submitError && <div className="inline-alert" role="alert"><AlertTriangle size={15}/>{submitError}</div>}
          <button className="button primary discovery-submit" disabled={!file || !consent || !readiness?.configured || submitting} onClick={() => void submit()}>{submitting ? <LoaderCircle className="spin" size={16}/> : <Braces size={16}/>} {submitting ? 'Agent 正在读取源码…' : readiness?.configured ? '开始动态发现' : '配置 DeepSeek 后可开始'}</button>
        </div>
      </section>
      <section className="panel discovery-history">
        <div className="panel-heading"><div><h2>发现记录</h2><p>记录源码摘要、只读工具轨迹、排除文件和逐行证据；不保存上传的源码包。</p></div><span>{items.length} 次</span></div>
        {!items.length ? <EmptyState title="还没有发现记录" description="配置 DeepSeek 后上传一个源码 ZIP。接口必须有真实文件与行号证据才能进入合同。"/> : <div className="discovery-records">{items.map(item => <article key={item.id} className="discovery-record">
          <header><div><strong>{item.archiveName}</strong><small>{item.id} · {new Date(item.createdAt).toLocaleString()}</small></div><span className={`status status-${item.status}`}><span/>{item.status === 'completed' ? '已完成' : item.status === 'failed' ? '失败' : '解析中'}</span></header>
          <div className="discovery-facts"><span><small>可读文件</small><strong>{item.fileManifest.length}</strong></span><span><small>安全排除</small><strong>{item.excludedFiles.length}</strong></span><span><small>工具调用</small><strong>{item.trace.length}</strong></span><span><small>发现接口</small><strong>{item.contract?.spec.interfaces.length ?? '—'}</strong></span></div>
          <details className="discovery-audit"><summary>查看审计详情</summary><dl><div><dt>源码摘要</dt><dd><code>{item.sourceDigest}</code></dd></div><div><dt>模型</dt><dd>{item.model}</dd></div></dl>{item.excludedFiles.length > 0 && <div><strong>安全排除</strong><ul>{item.excludedFiles.map(excluded => <li key={excluded.path}><code>{excluded.path}</code><span>{excluded.reason}</span></li>)}</ul></div>} {item.trace.length > 0 && <div><strong>只读工具轨迹</strong><ol>{item.trace.map((entry, index) => <li key={index}><code>{String(entry.tool || 'unknown')}</code><span>round {String(entry.round || '—')}</span></li>)}</ol></div>}</details>
          {item.error && <div className="inline-alert"><AlertTriangle size={15}/><span><strong>{item.error.code}</strong> · {item.error.message}</span></div>}
          {item.contract && <><div className="discovery-contract-heading"><strong>{item.contract.apiVersion}</strong><button className="button secondary compact-button" onClick={() => downloadContract(item)}><Download size={14}/>导出 JSON</button></div>
            {item.contract.spec.interfaces.length ? <div className="table-wrap"><table><thead><tr><th>接口</th><th>说明</th><th>请求 / 响应</th><th>认证 / 副作用</th><th>置信度</th><th>源码证据</th></tr></thead><tbody>{item.contract.spec.interfaces.map(found => <tr key={found.id}><td><strong className="method-pill">{found.method}</strong> <code>{found.path}</code></td><td>{found.summary || '—'}</td><td>{found.parameters.length} 参数 · {found.requestBody ? '有请求体' : '无请求体'} · {found.responses.length} 响应</td><td>{found.authentication.join(', ') || '未识别'} · {found.sideEffect}</td><td>{Math.round(found.confidence * 100)}%</td><td>{found.evidence.map(evidence => <code key={`${evidence.file}:${evidence.startLine}`}>{evidence.file}:{evidence.startLine}-{evidence.endLine}</code>)}</td></tr>)}</tbody></table></div> : <EmptyState title="未发现对外 HTTP 接口" description="Agent 已完成源码检查，但没有形成具备逐行证据的接口。"/>}
          </>}
        </article>)}</div>}
      </section>
    </>}
  </div>;
}
