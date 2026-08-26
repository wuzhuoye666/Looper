import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { SelectionMenu } from '../components/Layout';

function renderMenu(route = '/') {
  return render(<MemoryRouter initialEntries={[route]} future={{ v7_startTransition: true, v7_relativeSplatPath: true }}><SelectionMenu /></MemoryRouter>);
}

describe('侧栏选型分组导航', () => {
  it('按代码提供情况独立展开和收起功能入口', () => {
    renderMenu();
    const menu = screen.getByRole('button', { name: '选型' });
    expect(menu).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('button', { name: '可提供代码' })).not.toBeInTheDocument();

    fireEvent.click(menu);
    const withCode = screen.getByRole('button', { name: '可提供代码' });
    const withoutCode = screen.getByRole('button', { name: '不可提供代码' });
    fireEvent.click(withCode);
    expect(screen.getByRole('link', { name: '动态接口发现' })).toHaveAttribute('href', '/interfaces');
    expect(screen.getByRole('link', { name: '容量测试' })).toHaveAttribute('href', '/capacity');
    expect(screen.getByRole('link', { name: '系统配置优化' })).toHaveAttribute('href', '/system-optimization');
    expect(withoutCode).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(withoutCode);
    expect(screen.getByRole('link', { name: '场景目录' })).toHaveAttribute('href', '/benchmarks');
    expect(screen.getByRole('link', { name: '候选资源' })).toHaveAttribute('href', '/targets');
    expect(screen.queryByRole('link', { name: '实负载调优' })).not.toBeInTheDocument();
    fireEvent.click(withCode);
    expect(screen.queryByRole('link', { name: '动态接口发现' })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: '场景目录' })).toBeInTheDocument();
  });

  it('直接访问子页面时自动展开对应分类并高亮入口', () => {
    renderMenu('/capacity/study-1');
    expect(screen.getByRole('button', { name: '选型' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('button', { name: '可提供代码' })).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByRole('button', { name: '不可提供代码' })).toHaveAttribute('aria-expanded', 'false');
    expect(screen.getByRole('link', { name: '容量测试' })).toHaveClass('active');
  });
});
