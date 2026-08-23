# 动态接口发现：真实 DeepSeek QA 报告

- 日期：2026-08-23（Asia/Shanghai）
- 页面：`http://127.0.0.1:5173/interfaces`
- Provider / Model：DeepSeek / `deepseek-v4-flash`
- 输出合同：`looper.dev/interface-contract/v1`
- 最终截图：[`screenshots/source-discovery-live-five.png`](screenshots/source-discovery-live-five.png)
- 后端 Key 配置截图：[`screenshots/deepseek-key-backend-config.png`](screenshots/deepseek-key-backend-config.png)
- 安全边界：ZIP 经路径、符号链接、大小和敏感文件筛选后，只允许 Harness 使用 `list_files`、`search_code`、`read_file`；不执行上传代码，不访问源码声明的目标地址。

## 随机样本与结果

样本由 GitHub Search 返回集合中通过 `Get-Random` 选取。在下载 ZIP 前只记录仓库元数据，没有查看其源码；每次结果保存上传内容的 SHA-256、DeepSeek 工具轨迹和逐行证据。

| # | 仓库 | 语言 / 框架线索 | ZIP bytes | 终态 | 工具调用 | 接口数 |
|---|---|---|---:|---|---:|---:|
| 1 | [`DAMNDAGER/Image2-Studio`](https://github.com/DAMNDAGER/Image2-Studio) | Python / FastAPI | 40,892 | completed | 7 | 16 |
| 2 | [`SHAROZ221/CVE-Scanner`](https://github.com/SHAROZ221/CVE-Scanner) | Python / Flask | 236,753 | completed | 4 | 9 |
| 3 | [`colyseus/uWebSockets-express`](https://github.com/colyseus/uWebSockets-express) | TypeScript | 42,411 | completed | 17 | 67 |
| 4 | [`elizsir/poll-service`](https://github.com/elizsir/poll-service) | Go / Gin | 66,796 | completed | 28 | 15 |
| 5 | [`cosmiinn75/fitness-tracker-api`](https://github.com/cosmiinn75/fitness-tracker-api) | Java / Spring Boot | 186,737 | completed | 44 | 41 |

合计：5/5 completed，100 次受控源码工具调用，148 个接口。

## 真实调用暴露的问题与修复

| 严重度 | 实际故障 | 修复 / 结果 |
|---|---|---|
| High | `tool_choice=auto` 时模型可能不读源码直接输出 | 首轮强制 `tool_choice=required`，且没有工具轨迹时拒绝结果 |
| High | DeepSeek thinking 模式不支持强制工具选择，返回 HTTP 400 | 请求显式关闭 thinking；保留脱敏后的 provider code/message |
| High | Java 项目在 8K 输出上限被截断 | 识别 `finish_reason=length` 并返回明确错误；默认上限提高到 16K，最终得到 41 个接口 |
| High | 客户端断开或 API 重启会遗留 `running` 记录 | 捕获取消并在服务启动时把中断记录恢复为明确 failed 终态 |
| Medium | 模型输出的位置、schema、认证、置信度存在常见类型偏差 | 做有界别名/类型归一化，并最多执行两轮合同自修复；仍不合规则失败 |
| Medium | 文件输入控件未覆盖拖放区，点击部分区域不能弹出选择器 | 输入控件覆盖整个上传区；浏览器复验文件选择器可触发 |
| Medium | 页面需要操作员令牌，但错误后应用令牌不会自动重取数据 | 增加认证状态变化事件，接口发现页自动重载；浏览器清除并重新应用令牌后无需点“重试”即可显示记录 |

## 仍未解决的生产约束

- 当前上传 POST 同步等待 DeepSeek；第 5 个样本耗时约 144 秒。应改为创建后台任务后立即返回 discovery ID，由前端轮询或订阅状态。
- 接口发现只证明“从源码提取合同”，尚未执行容量测试。进入压测前还需要目标环境授权、鉴权数据、幂等/清理策略、写接口隔离、限流上限和停止条件。
- 模型结果必须以源码证据和合同校验为信任边界；不能把置信度当作运行时正确性的证明。

## 自动化验证

- Python：完整测试套件通过，Ruff 通过。
- Web：9 个测试文件、31 个测试通过，生产构建通过。
- DeepSeek：5 个随机仓库均由真实 provider 调用完成，不是 MockTransport 或静态解析结果。
- 密钥：未写入 `.env`、报告、源码或 Git；后续前端配置验证只写入被 Git 忽略的后端加密凭据文件。

## 后端保存 Key 复验

- 前端只在 React 内存中暂存输入，PUT 成功后立即清空；没有写入 localStorage/sessionStorage。
- Key 由后端保存到独立 Fernet 密文文件；Windows Fernet Key 再由当前服务账户 DPAPI 保护，Linux 文件限制为 0600。
- 配置 API 需要已配置且有效的操作员 Bearer token，只返回来源与后四位脱敏值。
- 停止 API 后，在 `LOOPER_DEEPSEEK_API_KEY` 为空的进程中重启，状态仍为 `source=stored`、`encryptedAtRest=true`。
- 重启后使用保存的 Key 对随机样本再次真实调用 DeepSeek：`discovery_e2990344198046e184a852427fe7ef2c` completed，16 个接口、4 次工具调用。

## 关联提交

- `bcc57e2 feat: add DeepSeek source interface discovery`
- `6f4f1a6 fix: close source discovery QA gaps`
- `a838b0c docs: record source discovery QA`
