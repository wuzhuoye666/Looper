import { Ban, CirclePause, Play, RefreshCw } from 'lucide-react';
import type { ExperimentStatus } from '../lib/types';

export type ExperimentAction = 'start' | 'pause' | 'resume' | 'cancel';
export function ExperimentActions({ status, busy, onAction }: { status: ExperimentStatus; busy?: boolean; onAction: (action: ExperimentAction) => void }) {
  return <div className="action-row">
    {(status === 'draft' || status === 'failed') && <button className="button primary" disabled={busy} onClick={() => onAction('start')}><Play size={15} />启动</button>}
    {status === 'running' && <button className="button secondary" disabled={busy} onClick={() => onAction('pause')}><CirclePause size={15} />暂停</button>}
    {status === 'paused' && <button className="button primary" disabled={busy} onClick={() => onAction('resume')}><RefreshCw size={15} />继续</button>}
    {['draft','queued','running','paused'].includes(status) && <button className="button danger-ghost" disabled={busy} onClick={() => onAction('cancel')}><Ban size={15} />取消</button>}
  </div>;
}
