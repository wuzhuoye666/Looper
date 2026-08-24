import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { InstanceTypeFacetFilter } from '../components/InstanceTypeFacetFilter';
import type { InstanceTypeFacets } from '../lib/types';

const families = Array.from({ length: 25 }, (_, index) => ({
  value: `g${index + 1}`,
  label: `通用型 g${index + 1}`,
  count: 1,
  generation: index + 1,
}));

const facets: InstanceTypeFacets = {
  architectures: [
    {
      value: 'x86',
      label: 'X86 计算',
      count: 26,
      types: [
        { value: 'general', label: '通用型', count: 25, families },
        { value: 'compute', label: '计算型', count: 1, families: [{ value: 'c9i', label: '计算型 c9i', count: 1, generation: 9 }] },
      ],
    },
    {
      value: 'arm',
      label: 'ARM 计算',
      count: 1,
      types: [{ value: 'general', label: '通用型', count: 1, families: [{ value: 'g8y', label: '通用型 g8y', count: 1, generation: 8 }] }],
    },
  ],
};

describe('InstanceTypeFacetFilter', () => {
  it('emits immediate single-select changes and clears incompatible children', () => {
    const onChange = vi.fn();
    render(<InstanceTypeFacetFilter facets={facets} value={{ architectureClass: 'x86', typeKind: 'general', familyToken: 'g25' }} onChange={onChange} />);

    fireEvent.click(screen.getByRole('button', { name: /ARM 计算/ }));
    expect(onChange).toHaveBeenLastCalledWith({ architectureClass: 'arm' });

    fireEvent.click(screen.getAllByRole('button', { name: /^全部$/ })[1]);
    expect(onChange).toHaveBeenLastCalledWith({ architectureClass: 'x86' });
  });

  it('shows 20 generations at first and loads another batch', () => {
    render(<InstanceTypeFacetFilter facets={facets} value={{ architectureClass: 'x86', typeKind: 'general' }} onChange={() => undefined} />);

    expect(screen.getByRole('button', { name: /通用型 g25/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /通用型 g5/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /加载更多规格族/ }));
    expect(screen.getByRole('button', { name: /通用型 g1/ })).toBeInTheDocument();
  });
});
