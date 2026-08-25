import { useEffect, useMemo, useState } from 'react';
import {
  Bar, BarChart, CartesianGrid, PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar,
  RadarChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import type { TooltipProps } from 'recharts';
import { EmptyState } from './States';
import { formatNumber } from '../lib/format';
import type { ScenarioComparison, ScenarioComparisonAxis, ScenarioComparisonTarget } from '../lib/types';

const SERIES = [
  { color: '#2878d4', dash: undefined },
  { color: '#d88725', dash: '7 4' },
  { color: '#2f9567', dash: '2 4' },
];

type ChartRow = { axis: string; axisKey: string; [key: string]: string | number | undefined };

function ComparisonTooltip({
  active, label, comparison, selectedTargets,
}: TooltipProps<number, string> & {
  comparison: ScenarioComparison;
  selectedTargets: ScenarioComparisonTarget[];
}) {
  if (!active || typeof label !== 'string') return null;
  const axis = comparison.axes.find(item => item.label === label);
  if (!axis) return null;
  return <div className="comparison-tooltip">
    <strong>{axis.label}</strong>
    <small>{axis.metric}</small>
    {selectedTargets.map((target, index) => {
      const value = target.values[axis.key];
      if (!value) return null;
      return <div key={target.targetId}>
        <i style={{ background: SERIES[index].color }} />
        <span>{target.label}</span>
        <b>{formatNumber(value.raw)} {axis.unit}</b>
        <em>{formatNumber(value.normalized, 1)}</em>
      </div>;
    })}
  </div>;
}

function leadPercent(axis: ScenarioComparisonAxis, winner: number, runnerUp: number) {
  if (runnerUp === 0) return undefined;
  return axis.direction === 'maximize'
    ? ((winner - runnerUp) / Math.abs(runnerUp)) * 100
    : ((runnerUp - winner) / Math.abs(runnerUp)) * 100;
}

export function ScenarioComparisonPanel({ comparisons = [] }: { comparisons?: ScenarioComparison[] }) {
  const [comparisonId, setComparisonId] = useState(comparisons[0]?.id || '');
  const comparison = comparisons.find(item => item.id === comparisonId) || comparisons[0];
  const defaultTargetIds = useMemo(
    () => comparison?.targets.slice(0, 3).map(item => item.targetId) || [],
    [comparison?.id],
  );
  const [selectedIds, setSelectedIds] = useState<string[]>(defaultTargetIds);

  useEffect(() => {
    if (comparison && comparison.id !== comparisonId) setComparisonId(comparison.id);
  }, [comparison, comparisonId]);

  useEffect(() => setSelectedIds(defaultTargetIds), [comparison?.id, defaultTargetIds]);

  if (!comparison) {
    return <EmptyState
      title="暂无同场景对比"
      description="至少需要同一场景下两台目标机的有效结果。"
    />;
  }

  const selectedTargets = comparison.targets.filter(target => selectedIds.includes(target.targetId));
  const rows: ChartRow[] = comparison.axes.map(axis => {
    const row: ChartRow = { axis: axis.label, axisKey: axis.key };
    selectedTargets.forEach((target, index) => {
      row[`series${index}`] = target.values[axis.key]?.normalized;
    });
    return row;
  });
  const differences = comparison.axes.map(axis => {
    const ranked = selectedTargets
      .map(target => ({ target, value: target.values[axis.key] }))
      .filter(item => item.value)
      .sort((a, b) => axis.direction === 'maximize'
        ? b.value.raw - a.value.raw
        : a.value.raw - b.value.raw);
    if (ranked.length < 2) return null;
    return {
      axis,
      winner: ranked[0],
      lead: leadPercent(axis, ranked[0].value.raw, ranked[1].value.raw),
      spread: Math.max(...ranked.map(item => item.value.normalized))
        - Math.min(...ranked.map(item => item.value.normalized)),
    };
  }).filter((item): item is NonNullable<typeof item> => item != null)
    .sort((a, b) => b.spread - a.spread)
    .slice(0, 3);

  const toggleTarget = (targetId: string) => {
    setSelectedIds(current => {
      if (current.includes(targetId)) return current.length <= 2 ? current : current.filter(id => id !== targetId);
      return current.length >= 3 ? current : [...current, targetId];
    });
  };
  const tooltip = <ComparisonTooltip comparison={comparison} selectedTargets={selectedTargets} />;

  return <>
    <div className="comparison-controls">
      <label><span>对比场景</span><select
        aria-label="选择对比场景"
        value={comparison.id}
        onChange={event => setComparisonId(event.target.value)}
      >{comparisons.map(item => <option key={item.id} value={item.id}>
        {item.scenarioName} · {item.benchmarkVersion}
      </option>)}</select></label>
      <div className="comparison-series" aria-label="对比目标">
        {comparison.targets.map(target => {
          const selectedIndex = selectedIds.indexOf(target.targetId);
          const selected = selectedIndex >= 0;
          const disabled = !selected && selectedIds.length >= 3;
          return <button
            type="button"
            key={target.targetId}
            aria-pressed={selected}
            disabled={disabled}
            onClick={() => toggleTarget(target.targetId)}
          >
            <i style={{ background: selected ? SERIES[selectedIndex].color : '#b8bec7' }} />
            {target.label}<small>{target.studyCount} 次研究</small>
          </button>;
        })}
      </div>
    </div>
    <div className="comparison-body">
      <div
        className="comparison-chart"
        role="img"
        data-chart-kind={comparison.axes.length >= 3 ? 'radar' : 'bar'}
        aria-label={`${comparison.scenarioName}目标机能力对比`}
      >
        {comparison.axes.length >= 3 ? <ResponsiveContainer width="100%" height={310}>
          <RadarChart
            data={rows}
            outerRadius="60%"
            margin={{ top: 24, right: 42, bottom: 24, left: 42 }}
          >
            <PolarGrid stroke="#dfe4ea" />
            <PolarAngleAxis
              dataKey="axis"
              tick={{ fill: '#59636f', fontSize: 11 }}
              tickFormatter={value => value.length > 12 ? `${value.slice(0, 11)}…` : value}
            />
            <PolarRadiusAxis domain={[0, 100]} tick={false} axisLine={false} />
            <Tooltip content={tooltip} />
            {selectedTargets.map((target, index) => <Radar
              key={target.targetId}
              name={target.label}
              dataKey={`series${index}`}
              stroke={SERIES[index].color}
              fill={SERIES[index].color}
              fillOpacity={0.1}
              strokeWidth={2}
              strokeDasharray={SERIES[index].dash}
              dot={{ r: index === 0 ? 3 : index === 1 ? 3.5 : 2.5, fill: SERIES[index].color }}
              isAnimationActive={false}
            />)}
          </RadarChart>
        </ResponsiveContainer> : <ResponsiveContainer width="100%" height={230}>
          <BarChart data={rows} layout="vertical" margin={{ top: 14, right: 24, bottom: 12, left: 20 }}>
            <CartesianGrid strokeDasharray="3 3" horizontal={false} stroke="#e5e8ec" />
            <XAxis type="number" domain={[0, 100]} tickLine={false} axisLine={false} fontSize={10} />
            <YAxis type="category" dataKey="axis" width={110} tickLine={false} axisLine={false} fontSize={11} />
            <Tooltip content={tooltip} />
            {selectedTargets.map((target, index) => <Bar
              key={target.targetId}
              name={target.label}
              dataKey={`series${index}`}
              fill={SERIES[index].color}
              radius={[0, 2, 2, 0]}
              isAnimationActive={false}
            />)}
          </BarChart>
        </ResponsiveContainer>}
      </div>
      <aside className="comparison-differences" aria-label="关键差异">
        <header><strong>关键差异</strong><span>按差距排序</span></header>
        {differences.length ? differences.map(item => <article key={item.axis.key}>
          <span>{item.axis.label}</span>
          <strong>{item.winner.target.label}</strong>
          <p>{formatNumber(item.winner.value.raw)} {item.axis.unit}</p>
          <em>{item.lead == null ? '领先幅度不可计算' : `领先 ${formatNumber(item.lead, 1)}%`}</em>
        </article>) : <p className="comparison-no-difference">当前选择没有足够的共同维度。</p>}
      </aside>
    </div>
  </>;
}
