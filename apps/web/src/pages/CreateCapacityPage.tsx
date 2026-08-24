import { useMutation, useQuery } from '@tanstack/react-query';
import { AlertTriangle, Braces, Gauge, LoaderCircle } from 'lucide-react';
import { FormEvent, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { BackLink } from '../components/Layout';
import { PageHeader } from '../components/PageHeader';
import { ErrorState, LoadingState } from '../components/States';
import { api } from '../lib/api';

export function CreateCapacityPage() {
  const { discoveryId = '' } = useParams();
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const discoveries = useQuery({ queryKey: ['source-discoveries'], queryFn: api.sourceDiscoveries });
  const discovery = useMemo(() => discoveries.data?.items.find(item => item.id === discoveryId), [discoveries.data, discoveryId]);
  const mutation = useMutation({
    mutationFn: () => api.createCapacityStudy(discoveryId, name.trim() || undefined),
    onSuccess: study => navigate(`/capacity/${encodeURIComponent(study.id)}`, { replace: true }),
  });
  function submit(event: FormEvent) { event.preventDefault(); mutation.mutate(); }
  if (discoveries.isLoading) return <div className="page"><LoadingState label="正在读取接口合同"/></div>;
  if (discoveries.isError) return <div className="page"><ErrorState error={discoveries.error} onRetry={() => discoveries.refetch()}/></div>;
  return <div className="page capacity-create-page">
    <BackLink to="/interfaces">返回动态接口发现</BackLink>
    <PageHeader title="创建容量测试" description="DeepSeek Agent 将重新读取加密保留的源码，生成可审核的隔离构建方案。"/>
    {!discovery ? <section className="notice warning"><AlertTriangle size={18}/><div><strong>接口发现记录不存在</strong><p>返回接口发现页重新选择记录。</p></div></section> : <form className="panel capacity-create-card" onSubmit={submit}>
      <div className="capacity-create-source"><Braces size={22}/><div><small>接口合同</small><strong>{discovery.archiveName}</strong><span>{discovery.contract?.spec.interfaces.length || 0} 个接口 · {discovery.sourceDigest}</span></div></div>
      <label><span>容量测试名称</span><input value={name} onChange={event => setName(event.target.value)} placeholder={`${discovery.archiveName} 容量测试`} maxLength={160}/></label>
      <div className="notice info"><Gauge size={18}/><div><strong>本步骤不会部署或运行代码</strong><p>Agent 只通过 list/search/read 工具生成 Dockerfile 与 Compose 草案；下一页必须由你审核。</p></div></div>
      {discovery.sourceArchive.status !== 'retained' && <div className="inline-alert"><AlertTriangle size={15}/>源码已过期，请返回发现记录重新上传相同摘要的 ZIP。</div>}
      {mutation.isError && <div className="inline-alert"><AlertTriangle size={15}/>{mutation.error instanceof Error ? mutation.error.message : '创建失败'}</div>}
      <button className="button primary" disabled={mutation.isPending || discovery.sourceArchive.status !== 'retained' || !discovery.contract?.spec.interfaces.length}>{mutation.isPending ? <LoaderCircle className="spin" size={16}/> : <Gauge size={16}/>} {mutation.isPending ? 'Agent 正在生成构建方案…' : '生成构建方案并进入向导'}</button>
    </form>}
  </div>;
}
