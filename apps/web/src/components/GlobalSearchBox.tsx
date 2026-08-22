import { useQuery } from '@tanstack/react-query';
import { Command, Search } from 'lucide-react';
import { useEffect, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../lib/api';

const labels: Record<string, string> = {
  experiment: '选型研究', benchmark: '场景', target: '候选资源', quote: '报价', order: '订单',
};

export function GlobalSearchBox() {
  const [value, setValue] = useState('');
  const [queryValue, setQueryValue] = useState('');
  const [focused, setFocused] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const navigate = useNavigate();
  const query = useQuery({
    queryKey: ['global-search', queryValue],
    queryFn: () => api.searchAll(queryValue),
    enabled: queryValue.length >= 2,
    staleTime: 15_000,
  });
  const open = focused && value.trim().length >= 2;
  const items = query.data?.items || [];
  const selectResult = (index: number) => {
    const item = items[index];
    if (!item) return;
    setFocused(false);
    navigate(item.url);
  };
  const onInputKeyDown = (event: ReactKeyboardEvent<HTMLInputElement>) => {
    if (!open) return;
    if (!items.length && ['ArrowDown', 'ArrowUp', 'Enter'].includes(event.key)) return;
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setActiveIndex(index => Math.min(index + 1, items.length - 1));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setActiveIndex(index => Math.max(index - 1, 0));
    } else if (event.key === 'Enter' && activeIndex >= 0) {
      event.preventDefault();
      selectResult(activeIndex);
    } else if (event.key === 'Escape') {
      setFocused(false);
    }
  };
  useEffect(() => {
    setActiveIndex(-1);
    const timer = window.setTimeout(() => setQueryValue(value.trim()), 200);
    return () => window.clearTimeout(timer);
  }, [value]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        document.getElementById('global-search')?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  return <div className="global-search">
    <label className="global-search-input" htmlFor="global-search">
      <Search size={16} /><span className="sr-only">全局搜索</span>
      <input id="global-search" role="combobox" aria-label="全局搜索" aria-expanded={open} aria-controls="global-search-results" aria-activedescendant={activeIndex >= 0 ? `global-search-option-${activeIndex}` : undefined} value={value} onChange={event => setValue(event.target.value)}
        onFocus={() => setFocused(true)} onBlur={() => window.setTimeout(() => setFocused(false), 150)} onKeyDown={onInputKeyDown}
        placeholder="搜索选型研究、场景、候选资源、报价和订单" />
      <kbd><Command size={12} />K</kbd>
    </label>
    {open && <div id="global-search-results" className="global-search-results" role="listbox" aria-label="搜索结果">
      {query.isLoading && <div className="global-search-empty">搜索中...</div>}
      {query.isError && <div className="global-search-empty">搜索暂时不可用</div>}
      {items.map((item, index) => <button id={`global-search-option-${index}`} role="option" aria-selected={activeIndex === index} key={`${item.type}-${item.id}`} className={`global-search-result ${activeIndex === index ? 'active' : ''}`} onMouseEnter={() => setActiveIndex(index)} onMouseDown={() => selectResult(index)}>
        <span className="search-type">{labels[item.type] || item.type}</span>
        <span><strong>{item.title}</strong><small>{item.subtitle || item.id}</small></span>
      </button>)}
      {query.data && !query.data.items.length && <div className="global-search-empty">没有匹配结果</div>}
    </div>}
  </div>;
}
