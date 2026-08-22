# 多云目录与采购

## 边界

Looper 的云资源市场统一了腾讯云 CVM、阿里云 ECS、火山引擎 ECS 和百度智能云 BCC 的目录、报价与按量创建接口。Provider 只接受显式环境变量凭证，不使用实例 metadata 或 SDK 默认凭证链。浏览器只能看到 SDK/凭证是否就绪和缺少的环境变量名，看不到凭证值。

开发和 CI 必须使用 fake provider。除非变更单明确授权，不得在自动化测试中设置 `LOOPER_LIVE_PURCHASE_ENABLED=true`。

## Provider 契约

每个 Provider 实现以下操作：

- `list_regions()` 与 `list_zones(region)`
- `search_instance_types(filters)` 与 `search_images(filters)`
- `quote(spec)`，返回金额、币种、过期时间和供应商事实
- `purchase(spec, client_token)`，返回供应商订单、请求 ID 和实例 ID

统一 launch spec 固定为 `postpaid`，包括地域、可用区、机型、镜像、数量、VPC、子网、安全组、密钥、系统盘、公网带宽和标签。报价的 `spec_digest` 与确认订单的 digest 必须一致。

| 能力 | 腾讯云 | 阿里云 | 火山引擎 | 百度智能云 |
| --- | --- | --- | --- | --- |
| 地域/可用区 | SDK | SDK | SDK | 地域端点表 + SDK 可用区 |
| 机型/镜像 | SDK | SDK | SDK | SDK |
| 库存提示 | 有，非锁定 | 有，非锁定 | 有，非锁定 | 无权威接口，显示未知 |
| 按量报价 | `InquiryPriceRunInstances`，公网使用固定带宽小时价 | `DescribePrice`，公网使用固定带宽小时价 | 只有通用 `BILLINGApi.query_price_for_pay_as_you_go`；ECS 到账号 Billing code 的映射未公开，默认阻断 | `get_price_by_spec`，标记预计；公网规格阻断 |
| 创建适配 | `RunInstances` | `RunInstances` | `RunInstances` | `create_instance_by_spec` |
| Looper live purchase | 就绪 | 就绪 | 阻断，等待 Billing code 映射 | 阻断，估价不覆盖完整创建规格 |
| 幂等 token | 支持 | 支持 | 支持 | 支持（询价 token 最多 63 字符） |
| dry-run | 无统一依赖 | SDK 支持 | SDK 支持 | 无 |

目录适配器使用各 SDK 的 offset、page number、next token 或 marker 分页。普通浏览按请求 limit 截断；需要本地名称/规格筛选时最多扫描 1000 条再返回匹配项，避免只搜索供应商首屏。目录与库存仍是建议信息，不是预留，供应商可在报价后、创建前拒绝库存。Looper 不把按流量计费的元/GB 伪装成小时金额：腾讯与阿里采用固定带宽后付费；百度公网需要独立 EIP/流量定价，因此当前在报价阶段 fail closed。

## 缓存语义

目录请求首先按 `provider + resource type + normalized filters` 计算 canonical digest。成功响应写入 `cloud_catalog_cache`，默认 300 秒内直接复用。实时请求失败时，86400 秒内的旧缓存可以作为 `stale-cache` 返回，并携带警告；超过 stale TTL 后错误会向操作者暴露。报价和购买永不使用目录缓存代替供应商报价。

## 订单状态机

1. `POST /cloud/quotes` 使用 `Idempotency-Key` 创建不可变报价快照。
2. `POST /cloud/orders/prepare` 绑定报价、launch spec、订单幂等键和持久化 provider client token，状态为 `awaiting_confirmation`。
3. 服务端生成短时 HMAC token 和包含厂商、实例名、金额的确认文本。
4. `POST /cloud/orders/{id}/confirm` 重新校验操作员身份、签名、过期时间、手工输入文本、金额、报价、全局开关、厂商 allowlist 和小时金额上限。
5. 对完全相同的 canonical spec 再次询价；金额、币种、有效性或“非估算”属性变化时废止旧报价并要求重新确认。
6. 通过条件 `UPDATE` 原子取得提交权，然后先提交 `submitting` 状态和稳定 client token，防止并发双调用及崩溃后误判为未调用。
7. 明确成功进入 `submitted` 并将实例写入 `targets`；明确失败进入终态 `failed`；超时或不可判定错误进入 `unknown`。

`failed` 和 `unknown` 都不能通过原确认接口重试。`failed` 必须重新询价；`unknown` 必须使用原 client token 到供应商控制台或查询 API 对账。已认证操作员可从专用 reconciliation context 读取稳定 client token 和 request ID，再调用 `/resolve` 以“已创建 + 实例 ID”或“确认未创建 + 备注”收口，Looper 会纳管实例并追加审计事件。控制平面启动时发现遗留 `submitting` 会将其转为 `unknown`，不会猜测请求未到达云侧。

订单详情提供报价到恢复的事件时间线，并可导出 `looper.cloud-order-evidence/v1` JSON。证据包含不可变 spec/quote digest、订单结果、事件幂等键和 provider 响应摘要；`evidenceDigest` 是去掉自身字段后对整份 manifest 计算的 canonical digest。原始 provider client token 只出现在 unknown 对账上下文，证据中仅保存其 SHA-256，确认 token 和确认原文不进入证据。

## 安全门禁

真实购买同时要求：

- `LOOPER_LIVE_PURCHASE_ENABLED=true`
- Provider 在 `LOOPER_LIVE_PURCHASE_PROVIDERS` 中且自身 `livePurchaseEnabled=true`
- `LOOPER_OPERATOR_TOKEN` 长度至少 32；敏感 API 要求 `Authorization: Bearer ...`
- `LOOPER_PURCHASE_CONFIRMATION_SECRET` 使用另一个至少 32 字符且非默认的随机值
- 报价不是估算价，报价和确认 token 均未过期
- 操作者手工输入完整确认文本，回显金额等于报价金额
- 总小时金额不超过 `LOOPER_MAX_LIVE_HOURLY_AMOUNT`
- quote 和 order 分别使用 8 到 160 字符的稳定幂等键

建议使用云厂商最小权限子账号：目录与询价权限常开，创建权限只在审批窗口临时授予。operator token、confirmation secret 和云密钥都应由进程级 secret manager 注入，不写日志或数据库备份。Web 只把 operator token 放在当前 tab 的 `sessionStorage`，通过自定义 Bearer 头发送；不使用认证 Cookie，因此跨站请求不能借用浏览器凭证。

## API

- `GET /api/v1/cloud/providers`
- `GET /api/v1/cloud/catalog/{provider}/{region|zone|instance-type|image}`
- `GET /api/v1/cloud/auth/status`
- `POST /api/v1/cloud/quotes`
- `GET /api/v1/cloud/quotes/{quote_id}`
- `POST /api/v1/cloud/orders/prepare`
- `GET /api/v1/cloud/orders`
- `GET /api/v1/cloud/orders/{order_id}`
- `GET /api/v1/cloud/orders/{order_id}/events`
- `GET /api/v1/cloud/orders/{order_id}/reconciliation-context`
- `GET /api/v1/cloud/orders/{order_id}/evidence`
- `POST /api/v1/cloud/orders/{order_id}/confirm`
- `POST /api/v1/cloud/orders/{order_id}/resolve`
- `GET /api/v1/search?q=...`

Swagger 请求仍受相同门禁保护。不要通过数据库修改订单状态或 token hash。

## 上线检查

1. 备份数据库并执行 `python -m alembic upgrade head`；确认 `alembic current` 为 head。
2. 以只读/询价权限启动 API，检查 `/cloud/providers` 的 SDK 与凭证状态。
3. 对每个启用地域检索地域、可用区、机型和镜像，核对账号配额和网络 ID。
4. 保持真实购买关闭，验证报价、准备订单、过期和错误展示。
5. 设置彼此独立的 operator token、confirmation secret、单一 Provider allowlist 和保守金额上限；通过 Web 钥匙入口验证 Bearer 认证。
6. 确认 Provider 状态明确为 `livePurchaseEnabled=true`；当前只允许完成账号级验证后的腾讯或阿里。
7. 在云厂商控制台开启费用预算、操作审计和创建告警后，再在短审批窗口打开全局开关。
8. 首单使用最低规格、数量 1，确认 Looper 订单、供应商订单和 `targets` 三处一致。
