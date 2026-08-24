import { useInfiniteQuery } from '@tanstack/react-query';
import { AlertTriangle, Check, ChevronLeft, ChevronRight, Cpu, Filter, Search, Server, Sparkles } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { api } from '../lib/api';
import { InstanceTypeFacetFilter } from './InstanceTypeFacetFilter';
import type {
  AdvisedCloudInstanceType,
  CloudInstanceType,
  CloudProviderId,
  CloudRegion,
  CloudZone,
  SelectionAdvisorRequest,
  SelectionScenario,
  InstanceSelectionClass,
} from '../lib/types';

const scenarios: Array<{ id: SelectionScenario; label: string; detail: string }> = [
  { id: 'web-api', label: 'Web / API', detail: '网站、网关和接口服务' },
  { id: 'microservices-rpc', label: '微服务 / RPC', detail: 'Java、Dubbo、服务间调用' },
  { id: 'database', label: '数据库', detail: 'MySQL、SQL Server、NoSQL' },
  { id: 'cache', label: '缓存', detail: 'Redis、Memcached' },
  { id: 'search-logs', label: '搜索与日志', detail: 'Elasticsearch、日志检索' },
  { id: 'big-data-messaging', label: '大数据 / 消息', detail: 'Hadoop、Spark、Kafka' },
  { id: 'game', label: '游戏', detail: '端游、手游和游戏逻辑' },
  { id: 'video', label: '视频', detail: '直播、转发和转码' },
  { id: 'ai', label: 'AI', detail: '训练或推理' },
  { id: 'development-test', label: '开发测试', detail: '非生产环境和临时任务' },
  { id: 'other', label: '其他', detail: '未归类的通用工作负载' },
];

const componentIds: SelectionScenario[] = [
  'web-api', 'microservices-rpc', 'database', 'cache', 'search-logs', 'big-data-messaging',
];

interface Answers {
  primaryScenario: SelectionScenario;
  coLocatedComponents: SelectionScenario[];
  exactCpu: number;
  exactMemoryGib: number;
  minimumGpuCount: number;
  localStorage: 'required' | 'not-required' | 'unknown';
  minimumNetworkBandwidthGbps: number;
  minimumNetworkPps: number;
  codeAvailability: 'available' | 'unavailable' | 'unknown';
  architecture: 'x86' | 'arm' | 'unknown';
}

const initialAnswers: Answers = {
  primaryScenario: 'web-api',
  coLocatedComponents: [],
  exactCpu: 0,
  exactMemoryGib: 0,
  minimumGpuCount: 0,
  localStorage: 'unknown',
  minimumNetworkBandwidthGbps: 0,
  minimumNetworkPps: 0,
  codeAvailability: 'unknown',
  architecture: 'unknown',
};

const stepLabels = ['使用场景', '资源需求', '部署约束', '推荐结果'];
const codeAvailabilityOptions = [
  { value: 'available', label: '可以提供', detail: '后续版本可进行兼容性分析' },
  { value: 'unavailable', label: '无法提供', detail: '候选结果会提示兼容性风险' },
  { value: 'unknown', label: '暂不确定', detail: '稍后仍可返回修改' },
] as const;

function architectureKind(value?: string) {
  const normalized = (value || '').toLowerCase().replace(/[_-]/g, '');
  if (normalized.includes('arm') || normalized.includes('aarch64')) return 'arm';
  if (normalized.includes('x86') || normalized.includes('amd64')) return 'x86';
  return 'unknown';
}

function formatNumber(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
}

function hasExactSizing(answers: Answers) {
  return answers.exactCpu > 0 && answers.exactMemoryGib > 0;
}

function isStillEligible(
  item: CloudInstanceType,
  answers: Answers,
  zone: string,
) {
  if (item.available === false) return false;
  if (hasExactSizing(answers) && (
    item.cpu !== answers.exactCpu || item.memoryGib !== answers.exactMemoryGib
  )) return false;
  if (answers.architecture !== 'unknown' && architectureKind(item.architecture) !== answers.architecture) return false;
  const capabilities = Array.isArray(item.attributes?.zoneCapabilities)
    ? item.attributes.zoneCapabilities.filter((value): value is Record<string, unknown> =>
      Boolean(value) && typeof value === 'object')
    : [];
  if (capabilities.length) {
    return capabilities.some(capability => {
      if (!zone ? capability.available !== true : capability.available === false) return false;
      if (Number(capability.gpu || 0) < answers.minimumGpuCount) return false;
      if (answers.localStorage === 'required' && !capability.localStorageCategory && !capability.localStorageCapacityGib) return false;
      if (answers.minimumNetworkBandwidthGbps > 0 && Number(capability.networkBandwidthGbps || 0) < answers.minimumNetworkBandwidthGbps) return false;
      if (answers.minimumNetworkPps > 0 && Number(capability.networkPps || 0) < answers.minimumNetworkPps) return false;
      return true;
    });
  }
  if ((item.gpu || 0) < answers.minimumGpuCount) return false;
  if (answers.localStorage === 'required' && !item.localStorageCount && !item.localStorageCategory) return false;
  if (answers.minimumNetworkBandwidthGbps > 0 && Math.min(
    item.networkBandwidthRxGbps || 0, item.networkBandwidthTxGbps || 0,
  ) < answers.minimumNetworkBandwidthGbps) return false;
  if (answers.minimumNetworkPps > 0 && Math.min(
    item.networkPpsRx || 0, item.networkPpsTx || 0,
  ) < answers.minimumNetworkPps) return false;
  return true;
}

export function CloudSelectionAdvisor({
  provider = 'alibaba',
  catalogAvailable = true,
  regions,
  zones,
  region,
  zone,
  onRegionChange,
  onZoneChange,
  selected,
  onSelect,
}: {
  provider?: Extract<CloudProviderId, 'alibaba' | 'tencent'>;
  catalogAvailable?: boolean;
  regions: CloudRegion[];
  zones: CloudZone[];
  region: string;
  zone: string;
  onRegionChange: (value: string) => void;
  onZoneChange: (value: string) => void;
  selected: CloudInstanceType | null;
  onSelect: (value: CloudInstanceType | null) => void;
}) {
  const [step, setStep] = useState(0);
  const [answers, setAnswers] = useState(initialAnswers);
  const [selectionNotice, setSelectionNotice] = useState('');
  const [candidateSearch, setCandidateSearch] = useState('');
  const [candidateQuery, setCandidateQuery] = useState('');
  const [architectureClass, setArchitectureClass] = useState<InstanceSelectionClass | undefined>();
  const [typeKind, setTypeKind] = useState<string | undefined>();
  const [familyToken, setFamilyToken] = useState<string | undefined>();
  const gpuRelevant = answers.primaryScenario === 'ai' || answers.primaryScenario === 'video';
  const localStorageRelevant = ['database', 'search-logs', 'big-data-messaging'].some(value =>
    value === answers.primaryScenario || answers.coLocatedComponents.includes(value as SelectionScenario));
  const networkRelevant = ['web-api', 'microservices-rpc', 'video'].includes(answers.primaryScenario);
  const providerName = provider === 'tencent' ? '腾讯云 CVM' : '阿里云 ECS';
  const providerEyebrow = provider === 'tencent' ? 'TENCENT CVM' : 'ALIYUN ECS';

  const request = useMemo<SelectionAdvisorRequest | null>(() => region ? {
    provider,
    region,
    zone: zone || undefined,
    primaryScenario: answers.primaryScenario,
    coLocatedComponents: answers.coLocatedComponents,
    sizingMode: hasExactSizing(answers) ? 'exact' : 'unknown',
    exactCpu: hasExactSizing(answers) ? answers.exactCpu : undefined,
    exactMemoryGib: hasExactSizing(answers) ? answers.exactMemoryGib : undefined,
    minimumGpuCount: gpuRelevant ? answers.minimumGpuCount : 0,
    localStorage: localStorageRelevant ? answers.localStorage : 'unknown',
    minimumNetworkBandwidthGbps: networkRelevant && answers.minimumNetworkBandwidthGbps > 0 ? answers.minimumNetworkBandwidthGbps : undefined,
    minimumNetworkPps: networkRelevant && answers.minimumNetworkPps > 0 ? answers.minimumNetworkPps : undefined,
    codeAvailability: answers.codeAvailability,
    architecture: answers.architecture,
    query: candidateQuery || undefined,
    architectureClass,
    typeKind,
    familyToken,
    offset: 0,
    limit: 20,
  } : null, [answers, architectureClass, candidateQuery, familyToken, gpuRelevant, localStorageRelevant, networkRelevant, provider, region, typeKind, zone]);

  const recommendations = useInfiniteQuery({
    queryKey: ['cloud-selection-advisor', provider, request],
    queryFn: ({ pageParam }) => api.selectionAdvisor({ ...request!, offset: pageParam, limit: 20 }),
    initialPageParam: 0,
    getNextPageParam: lastPage => lastPage.nextOffset ?? undefined,
    enabled: catalogAvailable && step === 3 && request !== null,
    staleTime: 30_000,
  });
  const pages = recommendations.data?.pages || [];
  const result = pages[0];
  const candidates = pages.flatMap(page => page.items);
  const candidateFiltersActive = Boolean(candidateQuery || architectureClass || typeKind || familyToken);

  useEffect(() => {
    setCandidateSearch('');
    setCandidateQuery('');
    setArchitectureClass(undefined);
    setTypeKind(undefined);
    setFamilyToken(undefined);
  }, [answers, provider, region, zone]);

  useEffect(() => {
    if (selected && !isStillEligible(selected, answers, zone)) {
      onSelect(null);
      setSelectionNotice(`已清除 ${selected.id}：修改后的硬约束不再匹配该机型。`);
    }
  }, [answers, onSelect, provider, selected, zone]);

  const chooseScenario = (value: SelectionScenario) => {
    setAnswers(current => ({
      ...current,
      primaryScenario: value,
      coLocatedComponents: [],
      minimumGpuCount: 0,
      localStorage: 'unknown',
      minimumNetworkBandwidthGbps: 0,
      minimumNetworkPps: 0,
    }));
    setStep(1);
  };
  const update = <K extends keyof Answers>(key: K, value: Answers[K]) => {
    setAnswers(current => ({ ...current, [key]: value }));
    setSelectionNotice('');
  };
  const toggleComponent = (value: SelectionScenario) => update(
    'coLocatedComponents',
    answers.coLocatedComponents.includes(value)
      ? answers.coLocatedComponents.filter(item => item !== value)
      : [...answers.coLocatedComponents, value].slice(0, 5),
  );
  const confirmCandidateSearch = () => setCandidateQuery(candidateSearch.trim());

  return <section className={`advisor-market-layout ${step < 3 ? 'questionnaire' : ''}`} aria-label={`${providerName} 选型助手`}>
    <aside className="panel selection-advisor">
      <div className="advisor-heading">
        <span><Sparkles size={17} /></span>
        <div><small>{providerEyebrow}</small><h2>选型助手</h2><p>只用硬约束排除，场景用于排序。</p></div>
      </div>
      <div className="advisor-progress" aria-label={`选型进度 ${Math.min(step + 1, 4)} / 4`}><span style={{ width: `${((Math.min(step, 3) + 1) / 4) * 100}%` }} /></div>
      <nav className="advisor-step-nav" aria-label="问卷步骤">
        {stepLabels.map((label, index) => <button key={label} type="button" className={index === step ? 'active' : index < step ? 'done' : ''} disabled={index > step} onClick={() => setStep(index)}><span>{index < step ? <Check size={11} /> : index + 1}</span>{label}</button>)}
      </nav>
      {!catalogAvailable && <div className="advisor-directory-warning" role="status"><AlertTriangle size={14} /><span>云厂商尚未连接。可以填写需求问卷，连接凭证后才能读取地域并生成候选。</span></div>}
      <div className="advisor-question">
        {step === 0 && <>
          <QuestionTitle title="主要使用场景是什么？" detail="选择最主要的工作负载，后续问题会随场景变化。" />
          <div className="advisor-choice-grid">{scenarios.map(item => <button type="button" key={item.id} onClick={() => chooseScenario(item.id)}><strong>{item.label}</strong><small>{item.detail}</small><ChevronRight size={14} /></button>)}</div>
        </>}
        {step === 1 && <>
          <QuestionTitle title="确认资源需求" detail="把容量和特殊资源一次填完；不确定的项目可以保持默认。" />
          <section className="advisor-question-section">
            <div className="advisor-section-heading"><h4>同机运行（可选）</h4><p>附加组件只影响推荐顺序，不会直接排除机型。</p></div>
            <div className="advisor-check-list">{componentIds.filter(id => id !== answers.primaryScenario).map(id => {
              const item = scenarios.find(value => value.id === id)!;
              return <label key={id} className={answers.coLocatedComponents.includes(id) ? 'selected' : ''}><input type="checkbox" checked={answers.coLocatedComponents.includes(id)} onChange={() => toggleComponent(id)} /><span><strong>{item.label}</strong><small>{item.detail}</small></span></label>;
            })}</div>
          </section>
          <section className="advisor-question-section">
            <div className="advisor-section-heading"><h4>CPU 与内存</h4><p>两项都填写时精确匹配；未填完整时不作为硬筛选条件。</p></div>
            <div className="advisor-fields"><label><span>vCPU</span><input aria-label="精确 vCPU" type="number" min={1} placeholder="可选" value={answers.exactCpu || ''} onChange={event => update('exactCpu', Number(event.target.value))} /></label><label><span>内存 GiB</span><input aria-label="精确内存 GiB" type="number" min={0.25} step={0.25} placeholder="可选" value={answers.exactMemoryGib || ''} onChange={event => update('exactMemoryGib', Number(event.target.value))} /></label></div>
          </section>
          <section className="advisor-question-section">
            <div className="advisor-section-heading"><h4>特殊资源（可选）</h4><p>只有明确填写的 GPU、本地盘和网络要求会排除机型。</p></div>
            {!gpuRelevant && !localStorageRelevant && !networkRelevant && <div className="advisor-neutral"><Server size={17} /><span>当前场景没有需要追加的特殊资源问题。</span></div>}
            {gpuRelevant && <label className="advisor-long-field"><span>最低 GPU 数量</span><select aria-label="最低 GPU 数量" value={answers.minimumGpuCount} onChange={event => update('minimumGpuCount', Number(event.target.value))}><option value={0}>不强制 GPU</option><option value={1}>至少 1 块</option><option value={2}>至少 2 块</option><option value={4}>至少 4 块</option><option value={8}>至少 8 块</option></select></label>}
            {localStorageRelevant && <div className="advisor-long-field"><span>是否强制本地盘？</span><div className="advisor-segmented triple"><button type="button" className={answers.localStorage === 'required' ? 'active' : ''} onClick={() => update('localStorage', 'required')}>必须</button><button type="button" className={answers.localStorage === 'not-required' ? 'active' : ''} onClick={() => update('localStorage', 'not-required')}>不需要</button><button type="button" className={answers.localStorage === 'unknown' ? 'active' : ''} onClick={() => update('localStorage', 'unknown')}>不清楚</button></div></div>}
            {networkRelevant && <div className="advisor-fields"><label><span>最低内网带宽 Gbit/s</span><input aria-label="最低内网带宽 Gbit/s" type="number" min={0} step={0.1} value={answers.minimumNetworkBandwidthGbps || ''} placeholder="不清楚可留空" onChange={event => update('minimumNetworkBandwidthGbps', Number(event.target.value))} /></label><label><span>最低网络 PPS</span><input aria-label="最低网络 PPS" type="number" min={0} value={answers.minimumNetworkPps || ''} placeholder="不清楚可留空" onChange={event => update('minimumNetworkPps', Number(event.target.value))} /></label></div>}
          </section>
          <QuestionActions back={() => setStep(0)} next={() => setStep(2)} nextLabel="继续设置部署约束" />
        </>}
        {step === 2 && <>
          <QuestionTitle title="确认部署约束" detail="选择代码状态、CPU 架构和部署位置，然后直接生成推荐。" />
          <section className="advisor-question-section">
            <div className="advisor-section-heading"><h4>代码可用性</h4><p>本期只记录状态，不上传、不读取，也不执行代码。</p></div>
            <div className="advisor-segmented triple advisor-code-options">{codeAvailabilityOptions.map(item => <button type="button" key={item.value} className={answers.codeAvailability === item.value ? 'active' : ''} onClick={() => update('codeAvailability', item.value)}>{item.label}</button>)}</div>
            <p className="advisor-code-hint">{codeAvailabilityOptions.find(item => item.value === answers.codeAvailability)?.detail}</p>
          </section>
          <section className="advisor-question-section">
            <div className="advisor-section-heading"><h4>架构与位置</h4><p>不清楚架构时保留 ARM 候选，但优先展示 x86。</p></div>
            <div className="advisor-long-field"><span>CPU 架构</span><div className="advisor-segmented triple"><button type="button" className={answers.architecture === 'x86' ? 'active' : ''} onClick={() => update('architecture', 'x86')}>x86</button><button type="button" className={answers.architecture === 'arm' ? 'active' : ''} onClick={() => update('architecture', 'arm')}>ARM</button><button type="button" className={answers.architecture === 'unknown' ? 'active' : ''} onClick={() => update('architecture', 'unknown')}>不清楚</button></div></div>
            <div className="advisor-fields"><label><span>地域 *</span><select aria-label="助手地域" value={region} onChange={event => onRegionChange(event.target.value)}><option value="">选择地域</option>{regions.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></label><label><span>可用区</span><select aria-label="助手可用区" value={zone} disabled={!region} onChange={event => onZoneChange(event.target.value)}><option value="">不限可用区</option>{zones.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}</select></label></div>
          </section>
          <QuestionActions back={() => setStep(1)} next={() => setStep(3)} disabled={!catalogAvailable || !region} nextLabel="查看推荐结果" />
        </>}
        {step === 3 && <>
          <QuestionTitle title="筛选已完成" detail="可返回任一步修改答案，候选列表会重新计算。" />
          <div className="advisor-answer-summary"><span><strong>{scenarios.find(item => item.id === answers.primaryScenario)?.label}</strong>主场景</span><span><strong>{hasExactSizing(answers) ? `${answers.exactCpu}C / ${answers.exactMemoryGib}G` : '待压测'}</strong>配置</span><span><strong>{answers.architecture === 'unknown' ? 'x86 优先' : answers.architecture.toUpperCase()}</strong>架构</span></div>
          <button type="button" className="button secondary advisor-edit" onClick={() => setStep(0)}><ChevronLeft size={14} />重新检查答案</button>
        </>}
      </div>
    </aside>

    {step === 3 && <div className="advisor-results">
      {recommendations.isLoading && <div className="panel advisor-placeholder"><Cpu className="spin" size={28} /><h2>正在读取{providerName}规格目录</h2><p>筛选当前地域的可售规格并计算匹配顺序。</p></div>}
      {recommendations.isError && <div className="panel advisor-placeholder error"><AlertTriangle size={28} /><h2>候选读取失败</h2><p>{recommendations.error instanceof Error ? recommendations.error.message : '请稍后重试'}</p><button type="button" className="button secondary" onClick={() => recommendations.refetch()}>重试</button></div>}
      {result && <>
        <section className="panel advisor-result-summary">
          <div><span className="eyebrow">FILTER RESULT</span><h2>{result.total ? `${candidateFiltersActive ? '匹配' : '剩余'} ${result.total} 个候选` : candidateFiltersActive && result.eligibleTotal ? '全部候选中没有匹配项' : '没有满足全部硬约束的机型'}</h2><p>{result.stale ? result.warning : `目录来源：${result.source === 'live' ? '实时' : '缓存'} · 每次加载 20 个`}</p></div>
          <div className="advisor-elimination">{result.exclusionStages.map(stage => <span key={stage.code}><small>{stage.label}</small><strong>{stage.before} → {stage.after}</strong></span>)}</div>
          {result.eligibleTotal > 0 && <InstanceTypeFacetFilter
            facets={result.instanceTypeFacets}
            value={{ architectureClass, typeKind, familyToken }}
            resetKey={`${provider}:${region}:${zone}:${JSON.stringify(answers)}`}
            onChange={value => {
              setArchitectureClass(value.architectureClass);
              setTypeKind(value.typeKind);
              setFamilyToken(value.familyToken);
            }}
          />}
          {result.eligibleTotal > 0 && <div className="advisor-candidate-search-wrap"><form className="search-submit-group advisor-candidate-search-form" onSubmit={event => { event.preventDefault(); confirmCandidateSearch(); }}><label className="search-field advisor-candidate-search"><Search size={16} /><span className="sr-only">搜索候选机型</span><input aria-label="搜索候选机型" value={candidateSearch} onChange={event => setCandidateSearch(event.target.value)} placeholder="搜索机型 ID、规格族或中文类型/分组" /></label><button type="submit" className="button primary search-confirm-button" disabled={candidateSearch.trim() === candidateQuery}>确认</button></form><small>{candidateSearch.trim() !== candidateQuery ? '内容尚未确认，当前结果保持不变' : candidateQuery ? `从 ${result.eligibleTotal} 个候选中匹配 ${result.total} 条，已显示 ${candidates.length} 条` : `已显示 ${candidates.length} / ${result.total} 条`}</small></div>}
          {!result.eligibleTotal && result.mostRestrictiveStage && <div className="advisor-zero-warning"><AlertTriangle size={15} /><span>限制最大的是“{result.mostRestrictiveStage.label}”，排除了 {result.mostRestrictiveStage.removed} 个机型。系统没有自动放宽条件。</span></div>}
          {selectionNotice && <div className="advisor-zero-warning"><AlertTriangle size={15} /><span>{selectionNotice}</span></div>}
        </section>
        {candidates.length > 0 && <section className="advisor-candidate-list">{candidates.map(item => <CandidateCard key={item.id} item={item} selected={selected?.id === item.id} onSelect={() => { onSelect(item); setSelectionNotice(''); }} />)}</section>}
        {candidateFiltersActive && result.eligibleTotal > 0 && !result.total && <div className="panel advisor-search-empty"><Filter size={22} /><strong>全部候选中没有匹配项</strong><span>请尝试其他分类、机型 ID 或规格族。</span></div>}
        {recommendations.hasNextPage && <button type="button" className="button secondary advisor-load-more" disabled={recommendations.isFetchingNextPage} onClick={() => recommendations.fetchNextPage()}>{recommendations.isFetchingNextPage ? '加载中…' : `加载更多（已显示 ${candidates.length} / ${result.total}）`}</button>}
      </>}
    </div>}
  </section>;
}

function QuestionTitle({ title, detail }: { title: string; detail: string }) {
  return <header className="advisor-question-title"><span className="eyebrow">当前步骤</span><h3>{title}</h3><p>{detail}</p></header>;
}

function QuestionActions({ back, next, disabled, nextLabel = '下一步' }: { back: () => void; next?: () => void; disabled?: boolean; nextLabel?: string }) {
  return <div className="advisor-actions"><button type="button" className="button secondary" onClick={back}><ChevronLeft size={14} />上一步</button>{next && <button type="button" className="button primary" disabled={disabled} onClick={next}>{nextLabel}<ChevronRight size={14} /></button>}</div>;
}

function CandidateCard({ item, selected, onSelect }: { item: AdvisedCloudInstanceType; selected: boolean; onSelect: () => void }) {
  const bandwidth = Math.min(item.networkBandwidthRxGbps || 0, item.networkBandwidthTxGbps || 0);
  const pps = Math.min(item.networkPpsRx || 0, item.networkPpsTx || 0);
  return <article className={`panel advisor-candidate ${selected ? 'selected' : ''}`}>
    <div className="candidate-heading"><span className={`match-tier ${item.matchTier}`}>{item.matchTier === 'preferred' ? '优先匹配' : item.matchTier === 'suitable' ? '适合' : '其他候选'}</span><span className={`stock-label ${item.available === true ? 'available' : 'unknown'}`}>{item.available === true ? '库存可用' : '库存未知'}</span></div>
    <h3>{item.id}</h3><p>{item.typeLabel || '其他类型'} · {item.familyLabel || `规格族 ${item.family || item.id}`} · {item.architecture || '架构未知'}</p>
    <div className="candidate-facts"><span><strong>{item.cpu}</strong>vCPU</span><span><strong>{formatNumber(item.memoryGib)}</strong>GiB 内存</span>{item.gpu ? <span><strong>{formatNumber(item.gpu)}</strong>GPU</span> : null}{bandwidth ? <span><strong>{formatNumber(bandwidth)}</strong>Gbit/s</span> : null}{pps ? <span><strong>{pps.toLocaleString()}</strong>PPS</span> : null}</div>
    <ul className="candidate-reasons">{item.reasons.map(reason => <li key={reason}><Check size={12} />{reason}</li>)}</ul>
    {item.warnings.length > 0 && <ul className="candidate-warnings">{item.warnings.map(warning => <li key={warning}><AlertTriangle size={12} />{warning}</li>)}</ul>}
    <button type="button" className={`button ${selected ? 'primary' : 'secondary'}`} onClick={onSelect}>{selected ? <><Check size={14} />已选择</> : '选择此机型'}</button>
  </article>;
}

export { CloudSelectionAdvisor as AlibabaSelectionAdvisor };
