import type { SelectionScenario } from './types';

export interface SelectionScenarioOption {
  id: SelectionScenario;
  label: string;
  detail: string;
}

export const SELECTION_SCENARIOS: SelectionScenarioOption[] = [
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
