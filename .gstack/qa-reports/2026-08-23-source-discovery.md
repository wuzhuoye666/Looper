# 动态接口发现前端 QA 报告

- 日期：2026-08-23（Asia/Shanghai）
- 页面：`http://127.0.0.1:5173/interfaces`
- 范围：标准层级，桌面浏览器 741px 窄视口
- DeepSeek 实际状态：未配置 `LOOPER_DEEPSEEK_API_KEY`
- 随机样本：[`yenanjing/awesome-model-routing`](https://github.com/yenanjing/awesome-model-routing)，GitHub Search 结果中通过 `Get-Random` 选取，下载 ZIP 19,814 bytes
- 修复后截图：[`screenshots/source-discovery-fixed.png`](screenshots/source-discovery-fixed.png)

## 5 条操作路径

| # | 操作方式 | 结果 | 发现 |
|---|---|---|---|
| 1 | 打开 `/interfaces`，检查未配置状态、数据流和合同版本 | 通过 | 配置缺失原因、环境变量、只读工具和禁止执行/目标网络均可见 |
| 2 | 文件选择器输入非 ZIP 的 `not-a-zip.py` | 通过 | 前端立即拒绝并说明不会解析 OpenAPI、单文件或其他压缩格式 |
| 3 | 文件选择器输入随机 GitHub 源码 ZIP，再勾选发送授权 | 阻断符合预期 | 未配置 DeepSeek 时按钮禁用；原实现仍允许选择文件和授权，造成无效操作 |
| 4 | 741px 视口通过汉堡菜单打开侧边栏抽屉 | 通过 | 抽屉层级和当前路由标识正确；原数据流横排过挤 |
| 5 | 刷新配置与历史，检查横向溢出 | 通过 | 历史为空态稳定，`scrollWidth == clientWidth`，无横向溢出 |

## 缺陷与修复

| 严重度 | 问题 | 修复 | 复验 |
|---|---|---|---|
| High | 接口合同只有 method/path，无法为后续容量测试生成请求 | 增加参数、请求体、响应结构；HTTP 方法和副作用枚举；接口 ID 由接口与证据摘要稳定生成 | 后端协议测试通过 |
| Medium | DeepSeek 未配置时仍可选文件和勾选授权，但永远无法提交 | 锁定上传区和授权框，按钮改为“配置 DeepSeek 后可开始” | 浏览器确认三个控件均处于阻断态 |
| Medium | 页面声明保留排除项和工具轨迹，但记录卡片没有查看入口 | 增加可展开审计详情，展示源码摘要、模型、安全排除和只读工具轮次 | 前端构建和类型检查通过 |
| Medium | 741px 窄屏下三段数据流横排，文字密度过高 | 760px 以下改为纵向流程和旋转箭头，统计块改为两列 | 浏览器确认单列流程且无横向溢出 |

## 验证

- Python：完整测试套件通过（100%），Ruff 通过。
- Web：9 个测试文件、30 个测试全部通过，生产构建通过。
- DeepSeek Harness：使用 `httpx.MockTransport` 验证真实官方协议形状（`tool_calls`、`tool_call_id`、`response_format=json_object`）与服务端证据校验。
- 未验证项：当前机器没有 DeepSeek API Key，因此没有声称真实 DeepSeek 推理成功，也没有产生伪造的接口发现记录。

## 提交

- `bcc57e2 feat: add DeepSeek source interface discovery`
- `6f4f1a6 fix: close source discovery QA gaps`
