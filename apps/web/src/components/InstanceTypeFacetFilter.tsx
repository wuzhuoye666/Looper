import { useEffect, useMemo, useState, type ReactNode } from 'react';
import type {
  InstanceSelectionClass,
  InstanceTypeFacets,
  InstanceTypeFamilyFacet,
  InstanceTypeKindFacet,
} from '../lib/types';

const FAMILY_BATCH_SIZE = 20;

export interface InstanceTypeFacetValue {
  architectureClass?: InstanceSelectionClass;
  typeKind?: string;
  familyToken?: string;
}

function naturalParts(value: string) {
  return value.split(/(\d+)/).filter(Boolean).map(part => /^\d+$/.test(part) ? Number(part) : part.toLocaleLowerCase());
}

function naturalCompare(left: string, right: string) {
  const a = naturalParts(left);
  const b = naturalParts(right);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    if (a[index] === undefined) return -1;
    if (b[index] === undefined) return 1;
    if (a[index] === b[index]) continue;
    if (typeof a[index] === typeof b[index]) return a[index] < b[index] ? -1 : 1;
    return typeof a[index] === 'number' ? -1 : 1;
  }
  return 0;
}

function familyCompare(left: InstanceTypeFamilyFacet, right: InstanceTypeFamilyFacet) {
  if (left.generation != null && right.generation == null) return -1;
  if (left.generation == null && right.generation != null) return 1;
  if (left.generation !== right.generation) return (right.generation || 0) - (left.generation || 0);
  return naturalCompare(left.value, right.value);
}

function mergeTypes(facets: InstanceTypeFacets, architecture?: InstanceSelectionClass) {
  const result = new Map<string, InstanceTypeKindFacet>();
  facets.architectures
    .filter(item => !architecture || item.value === architecture)
    .flatMap(item => item.types)
    .forEach(item => {
      const current = result.get(item.value);
      if (!current) result.set(item.value, { ...item, families: [...item.families] });
      else result.set(item.value, { ...current, count: current.count + item.count, families: [...current.families, ...item.families] });
    });
  return [...result.values()];
}

function mergeFamilies(types: InstanceTypeKindFacet[], typeKind?: string) {
  const result = new Map<string, InstanceTypeFamilyFacet>();
  types.filter(item => !typeKind || item.value === typeKind).flatMap(item => item.families).forEach(item => {
    const current = result.get(item.value);
    result.set(item.value, current ? { ...current, count: current.count + item.count } : item);
  });
  return [...result.values()].sort(familyCompare);
}

export function InstanceTypeFacetFilter({ facets, value, onChange, resetKey }: {
  facets?: InstanceTypeFacets;
  value: InstanceTypeFacetValue;
  onChange: (value: InstanceTypeFacetValue) => void;
  resetKey?: string;
}) {
  const [familyLimit, setFamilyLimit] = useState(FAMILY_BATCH_SIZE);
  const types = useMemo(() => facets ? mergeTypes(facets, value.architectureClass) : [], [facets, value.architectureClass]);
  const families = useMemo(() => mergeFamilies(types, value.typeKind), [types, value.typeKind]);
  useEffect(() => setFamilyLimit(FAMILY_BATCH_SIZE), [value.architectureClass, value.typeKind, resetKey]);
  useEffect(() => {
    if (!facets) return;
    if (value.architectureClass && !facets.architectures.some(item => item.value === value.architectureClass)) {
      onChange({});
      return;
    }
    if (value.typeKind && !types.some(item => item.value === value.typeKind)) {
      onChange({ architectureClass: value.architectureClass });
      return;
    }
    if (value.familyToken && !families.some(item => item.value === value.familyToken)) {
      onChange({ architectureClass: value.architectureClass, typeKind: value.typeKind });
    }
  }, [facets, families, onChange, types, value.architectureClass, value.familyToken, value.typeKind]);

  if (!facets?.architectures.length) return null;
  const visibleFamilies = families.slice(0, familyLimit);
  const selectedFamily = families.find(item => item.value === value.familyToken);
  if (selectedFamily && !visibleFamilies.some(item => item.value === selectedFamily.value)) visibleFamilies.push(selectedFamily);

  return <section className="panel instance-facet-filter" aria-label="机型分类筛选">
    <FacetRow label="计算架构">
      <FacetButton active={!value.architectureClass} label="全部" onClick={() => onChange({})} />
      {facets.architectures.filter(item => item.count > 0).map(item => <FacetButton key={item.value} active={value.architectureClass === item.value} label={item.label} count={item.count} onClick={() => onChange({ architectureClass: item.value })} />)}
    </FacetRow>
    <FacetRow label="实例类型">
      <FacetButton active={!value.typeKind} label="全部" onClick={() => onChange({ architectureClass: value.architectureClass })} />
      {types.filter(item => item.count > 0).map(item => <FacetButton key={item.value} active={value.typeKind === item.value} label={item.label} count={item.count} onClick={() => onChange({ architectureClass: value.architectureClass, typeKind: item.value })} />)}
    </FacetRow>
    <FacetRow label="规格族">
      <FacetButton active={!value.familyToken} label="全部" onClick={() => onChange({ architectureClass: value.architectureClass, typeKind: value.typeKind })} />
      {visibleFamilies.filter(item => item.count > 0).map(item => <FacetButton key={item.value} active={value.familyToken === item.value} label={item.label} count={item.count} onClick={() => onChange({ ...value, familyToken: item.value })} />)}
      {families.length > familyLimit && <button type="button" className="facet-load-more" onClick={() => setFamilyLimit(current => current + FAMILY_BATCH_SIZE)}>加载更多规格族（{Math.min(familyLimit, families.length)} / {families.length}）</button>}
    </FacetRow>
  </section>;
}

function FacetRow({ label, children }: { label: string; children: ReactNode }) {
  return <div className="instance-facet-row"><strong>{label}</strong><div>{children}</div></div>;
}

function FacetButton({ active, label, count, onClick }: { active: boolean; label: string; count?: number; onClick: () => void }) {
  return <button type="button" className={active ? 'active' : ''} aria-pressed={active} onClick={onClick}><span>{label}</span>{count !== undefined && <small>{count}</small>}</button>;
}
