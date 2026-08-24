import { ArrowDownToLine, Clipboard, LoaderCircle, Terminal, Wifi, WifiOff } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { API_BASE, resolveApiUrl } from '../lib/api';

type TerminalStream = 'command' | 'stdout' | 'stderr';
type ConnectionState = 'connecting' | 'live' | 'reconnecting';

interface ExperimentEvent {
  sequence: number;
  type: string;
  entityType: string;
  entityId: string;
  payload?: Record<string, unknown>;
  createdAt: string;
}

interface TerminalLine {
  id: string;
  stream: TerminalStream;
  stage: string;
  text: string;
  createdAt: string;
}

const eventNames = [
  'attempt.log',
];

const streamLabels: Record<TerminalStream, string> = {
  command: 'command', stdout: 'stdout', stderr: 'stderr',
};

const stageLabels: Record<string, string> = {
  prepare: '环境准备', warmup: '预热', run: '实际测试', normalize: '结果整理',
  validate: '结果校验', collect: '证据收集', cleanup: '清理环境',
};

export function ExperimentTerminal({ experimentId }: { experimentId: string }) {
  const [lines, setLines] = useState<TerminalLine[]>([]);
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [autoScroll, setAutoScroll] = useState(true);
  const sequenceRef = useRef(0);
  const viewportRef = useRef<HTMLDivElement>(null);
  const linesRef = useRef<TerminalLine[]>([]);

  useEffect(() => { linesRef.current = lines; }, [lines]);

  useEffect(() => {
    setLines([]);
    linesRef.current = [];
    sequenceRef.current = 0;
    const url = resolveApiUrl(API_BASE + '/experiments/' + encodeURIComponent(experimentId) + '/events');
    url.searchParams.set('after', '0');
    const source = new EventSource(url.toString());
    const onEvent = (raw: Event) => {
      const event = raw as MessageEvent<string>;
      let payload: ExperimentEvent;
      try { payload = JSON.parse(event.data) as ExperimentEvent; } catch { return; }
      if (payload.sequence <= sequenceRef.current) return;
      sequenceRef.current = payload.sequence;
      const next = eventToLines(payload);
      if (!next.length) return;
      const merged = [...linesRef.current, ...next].slice(-600);
      linesRef.current = merged;
      setLines(merged);
    };
    source.onopen = () => setConnection('live');
    source.onerror = () => setConnection('reconnecting');
    eventNames.forEach(name => source.addEventListener(name, onEvent));
    return () => {
      eventNames.forEach(name => source.removeEventListener(name, onEvent));
      source.close();
    };
  }, [experimentId]);

  useEffect(() => {
    if (autoScroll && viewportRef.current) viewportRef.current.scrollTop = viewportRef.current.scrollHeight;
  }, [autoScroll, lines]);

  const status = useMemo(() => {
    if (connection === 'live') return { label: '实时连接', className: 'live', icon: <Wifi size={14} /> };
    if (connection === 'reconnecting') return { label: '正在重连', className: 'reconnecting', icon: <LoaderCircle size={14} /> };
    return { label: '正在连接', className: 'connecting', icon: <WifiOff size={14} /> };
  }, [connection]);

  const copyOutput = async () => { await navigator.clipboard?.writeText(lines.map(line => line.text).join('')); };

  return <section className="panel experiment-terminal" aria-label="远端原始进程输出">
    <div className="experiment-terminal-heading">
      <div className="panel-heading-copy"><div className="terminal-title"><Terminal size={18} /><div><h2>远端原始输出</h2><p>Worker 从目标机命令的 stdout / stderr 原样分块透传，不改写、不摘要</p></div></div></div>
      <div className="terminal-actions">
        <span className={'terminal-connection ' + status.className}>{status.icon}{status.label}</span>
        <button type="button" className={'icon-button' + (autoScroll ? ' active' : '')} title="自动滚动" aria-label="自动滚动" aria-pressed={autoScroll} onClick={() => setAutoScroll(value => !value)}><ArrowDownToLine size={15} /></button>
        <button type="button" className="icon-button" title="复制终端输出" aria-label="复制终端输出" onClick={copyOutput} disabled={!lines.length}><Clipboard size={15} /></button>
      </div>
    </div>
    <div className="terminal-toolbar"><span>{lines.length ? lines.length + ' 个原始输出块' : '等待远端命令输出…'}</span><code>command · stdout · stderr · SSE</code></div>
    <div className="terminal-viewport" ref={viewportRef} role="log" aria-live="polite">
      {!lines.length && <div className="terminal-empty"><LoaderCircle size={18} /><span>等待 Worker 透传目标机命令和原始输出</span></div>}
      {lines.map(line => <div className={'terminal-line ' + line.stream} key={line.id}>
        <span className="terminal-line-meta"><b>{streamLabels[line.stream]}</b><small>{stageLabels[line.stage] || line.stage}</small></span>
        <pre>{line.text}</pre>
      </div>)}
    </div>
  </section>;
}

export function eventToLines(event: ExperimentEvent): TerminalLine[] {
  const payload = event.payload || {};
  const timestamp = event.createdAt;
  if (event.type === 'attempt.log') {
    const stream = payload.stream;
    if (!isStream(stream) || typeof payload.text !== 'string') return [];
    return [{ id: String(event.sequence) + ':log', stream, stage: String(payload.stage || 'run'), text: payload.text, createdAt: timestamp }];
  }
  return [];
}

function isStream(value: unknown): value is TerminalStream {
  return value === 'command' || value === 'stdout' || value === 'stderr';
}
