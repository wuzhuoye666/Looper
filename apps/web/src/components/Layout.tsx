import { Activity, Boxes, Braces, ChevronDown, ChevronLeft, Cloud, Code2, Gauge, GitCompareArrows, Menu, Plus, Server, SlidersHorizontal, TestTubeDiagonal, WandSparkles } from 'lucide-react';
import { useLayoutEffect, useState } from 'react';
import { resolveApiUrl } from '../lib/api';
import { NavLink, Outlet, useLocation } from 'react-router-dom';
import { GlobalSearchBox } from './GlobalSearchBox';
import { OperatorAccessButton, OperatorAccessProvider } from './OperatorAccess';

const nav = [
  { to: '/', label: '总览', icon: Gauge, end: true },
  { to: '/experiments', label: '选型研究', icon: GitCompareArrows },
  { to: '/cloud/market', label: '云资源市场', icon: Cloud },
];

const selectionGroups = [
  {
    id: 'with-code', label: '可提供代码', icon: Code2,
    items: [
      { to: '/interfaces', label: '动态接口发现', icon: Braces },
      { to: '/capacity', label: '容量测试', icon: TestTubeDiagonal },
      { to: '/system-optimization', label: '系统配置优化', icon: WandSparkles },
    ],
  },
  {
    id: 'without-code', label: '不可提供代码', icon: Boxes,
    items: [
      { to: '/benchmarks', label: '场景目录', icon: Boxes },
      { to: '/targets', label: '候选资源', icon: Server },
      { to: '/optimization', label: '实负载调优', icon: SlidersHorizontal },
    ],
  },
] as const;

function matchesRoute(pathname: string, route: string) {
  return pathname === route || pathname.startsWith(`${route}/`);
}

export function SelectionMenu({ onNavigate }: { onNavigate?: () => void }) {
  const { pathname } = useLocation();
  const activeGroup = selectionGroups.find(group => group.items.some(item => matchesRoute(pathname, item.to)))?.id;
  const [open, setOpen] = useState(Boolean(activeGroup));
  const [groupState, setGroupState] = useState<Record<string, boolean>>(() => activeGroup ? { [activeGroup]: true } : {});

  useLayoutEffect(() => {
    if (!activeGroup) return;
    setOpen(true);
    setGroupState(current => ({ ...current, [activeGroup]: true }));
  }, [activeGroup]);

  return <div className={`selection-menu ${open ? 'open' : ''}`}>
    <button className="selection-menu-toggle" type="button" aria-expanded={open} aria-controls="selection-menu-groups" onClick={() => setOpen(current => !current)}>
      <GitCompareArrows size={17} /><span>选型</span><ChevronDown className="selection-chevron" size={16} />
    </button>
    {open && <div className="selection-menu-groups" id="selection-menu-groups">
      {selectionGroups.map(({ id, label, icon: GroupIcon, items }) => {
        const groupOpen = Boolean(groupState[id]);
        return <section className="selection-menu-group" key={id}>
          <button className="selection-group-toggle" type="button" aria-expanded={groupOpen} aria-controls={`selection-group-${id}`} onClick={() => setGroupState(current => ({ ...current, [id]: !current[id] }))}>
            <GroupIcon size={16} /><span>{label}</span><ChevronDown className="selection-chevron" size={15} />
          </button>
          {groupOpen && <div className="selection-group-links" id={`selection-group-${id}`}>
            {items.map(({ to, label: itemLabel, icon: ItemIcon }) => <NavLink key={to} to={to} onClick={onNavigate}>
              <ItemIcon size={15} /><span>{itemLabel}</span>
            </NavLink>)}
          </div>}
        </section>;
      })}
    </div>}
  </div>;
}

export function Layout() {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  useLayoutEffect(() => {
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;
  }, [pathname]);
  return <OperatorAccessProvider><div className="app-shell">
    <aside className={`sidebar ${open ? 'open' : ''}`} aria-label="应用侧边栏">
      <div className="brand"><div className="brand-mark"><Activity size={19} /></div><div><strong>Looper</strong><span>服务器选型套件</span></div></div>
      <nav aria-label="主导航">{nav.map(({ to, label, icon: Icon, end }) => <NavLink key={to} to={to} end={end} onClick={() => setOpen(false)}><Icon size={18} /><span>{label}</span></NavLink>)}</nav>
      <div className="sidebar-bottom"><SelectionMenu onNavigate={() => setOpen(false)} /><div className="system-state"><span /><div><strong>控制平面正常</strong><small>API · {resolveApiUrl(import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').host}</small></div></div></div>
    </aside>
    {open && <button className="drawer-overlay" aria-label="关闭导航" onClick={() => setOpen(false)} />}
    <main className="main-shell"><div className="mobile-bar"><button className="icon-button" onClick={() => setOpen(true)} aria-label="打开导航"><Menu size={20} /></button><strong>Looper</strong><div className="mobile-actions"><OperatorAccessButton /><NavLink className="icon-button" to="/experiments/new" aria-label="新建选型研究"><Plus size={20} /></NavLink></div></div><header className="global-topbar"><GlobalSearchBox /><div className="topbar-actions"><OperatorAccessButton /><div className="topbar-context"><span className="live-dot" />本地控制平面</div></div></header><Outlet /></main>
  </div></OperatorAccessProvider>;
}

export function BackLink({ to, children }: { to: string; children: React.ReactNode }) { return <NavLink className="back-link" to={to}><ChevronLeft size={16} />{children}</NavLink>; }
