# 本机功能开发与验收

更新：2026-10-27。此环境只用于合成数据联调，不依赖 Apple 开发者账号或远程测试服务器。所有金额、报告与 AI 回答均明确标记为本机合成数据。

## 启动与会话

在后端仓库运行（需要 Docker Desktop 和 uv）：

```sh
uv run python scripts/simulator.py start
uv run python scripts/smoke-simulator.py
```

`start` 构建当前源码，启动独立 `reflection-simulator` Compose 项目，迁移数据库，创建 `LOCAL300` 合成持仓与报价、`SIMNAV` 合成基金净值及涨跌、报告、本地 AI 授权。API 仅监听 `127.0.0.1:8000`；PostgreSQL/Redis 不向宿主暴露端口。模型采用确定性工具调用夹具，不会请求 DeepSeek 或其他外部处理方；生产配置禁止启用该模式。它验证任务、工具和界面链路，不代表真实模型质量或供应商覆盖。

先在 iOS 仓库构建 Debug 并安装，再签发普通 Sandbox 用户会话：

```sh
xcodebuild -project PortfolioAgent.xcodeproj -scheme PortfolioAgent \
  -configuration Debug -destination 'generic/platform=iOS Simulator' \
  -derivedDataPath DerivedData CODE_SIGN_IDENTITY=- build
xcrun simctl list devices booted
xcrun simctl install <UDID> DerivedData/Build/Products/Debug-iphonesimulator/PortfolioAgent.app
```

回到后端仓库：

```sh
uv run python scripts/simulator.py session --simulator <UDID>
```

脚本把一次性会话放入已安装 App 的沙盒并启动 `--beta-cloud-only`。读取后删除导入文件；凭据存入 Keychain。会话与管理员令牌保存在忽略提交的 `secrets/simulator/`，权限 0600，不打印到日志；管理员令牌不进入 iOS。重新签发会撤销这个合成用户的旧会话，因此同时运行 HTTP 测试和 iOS 时应避免再次签发。

```sh
uv run python scripts/simulator.py status
uv run python scripts/simulator.py revoke
uv run python scripts/simulator.py stop
# 只有需要彻底丢弃本机合成数据时执行；不涉及远程或默认开发库
uv run python scripts/simulator.py reset --erase-local-fixtures
```

`stop` 保留数据卷。`reset` 要求显式参数，仅删除此独立 Compose 项目的卷；本轮未执行清卷验收。

## 功能走查清单

自动化证据位于本机忽略提交的 `artifacts/verification-2026-10-26/` 与 `artifacts/verification-2026-10-27/`。复现步骤和通过范围保留在此文档。

| 功能 | 操作与预期 | 本轮证据 |
|---|---|---|
| 启动 | start 后服务健康、迁移完成，iOS 总览显示本机合成持仓 | 已通过，iPhone 18 Pro / iOS 27 |
| 问答 | Ask Agent → 推荐问题 → 发送；等待后出现标注合成的回答 | 已通过 UI；`ask.png`；本机 HTTP smoke 也通过 |
| 历史会话 | 历史列表显示新问题，点击恢复消息 | 列表与消息恢复已通过 |
| 幂等重试 | 同键发送不产生第二个 run；客户端网络结果不确定时保留原问题和键 | 后端 HTTP smoke、`CloudChatTests` 通过 |
| 报告 | 总览打开报告，显示快照、阅读状态；进入追问并得到回答 | HTTP 详情/阅读/追问通过；模拟器快照/阅读/实际追问完成已检查 |
| 交易历史 | 从账户/持仓详情进入，区分初始快照与交易，显示日期/费用；加载更多 | API 权限/过滤/时间分页通过；模拟器初始快照、数量、价格/费用已检查 |
| 分类与顺序 | 核心/卫星切换，调整首页顺序，重启读回；旧版本返回冲突 | 后端冲突测试通过；UI 待完成 |
| 纪律 | 两个编辑器使用同一初始版本，后保存者收到冲突，不覆盖前者 | 后端测试通过；UI 待完成 |
| AI 授权 | 查看处理方/披露版本；交互和定时分别撤回；账本仍可使用 | Worker 撤回测试通过；UI 待完成 |
| 偏好 | 每日/每周开关保存后退出再进入读回 | 模拟器每日开关保存、重进读回及恢复原值通过 |
| 导出 | 导出 14 类数据到 JSON，无凭据；离开页面删除临时文件 | API 隔离、Swift JSON 通过；模拟器实际导出文件 14 类记录验证，离页后文件已清理 |
| 删除 | 重新认证后输入 DELETE；会话立即失效；回执查询最终状态 | 后端删除、trace/记忆/监察表清理通过；UI 待完成 |
| Trace | 运维角色查询 run；llm → tool → llm、参数脱敏；普通用户拒绝 | HTTP 与权限/保留期通过；Streamlit AppTest 时间线、工具筛选和空状态通过 |
| 图表 | 模型请求本轮 K 线，再标注；只有真实点位能成为趋势线端点 | 后端当前轮/越界测试与 iOS 构建通过；UI 待完成 |
| 异常状态 | 无数据、缺价/FX、断网重连、会话撤销后不回 Demo | 逻辑测试部分覆盖；完整 UI 待完成 |
| 布局 | 小屏、横屏、大字号、长标题/金额、滑动切页及键盘输入可用 | 待完整验收 |

## ReAct 与部署边界

默认图在 Celery prefork Worker 内构建；API 不导入 LangChain。预算参数为 `PA_AGENT_MAX_ROUNDS`（12）、`PA_AGENT_TIMEOUT_SECONDS`（95）、`PA_AGENT_TOOL_TIMEOUT_SECONDS`（15）、`PA_AGENT_MAX_TOKENS`（64000）、`PA_AGENT_RESULT_MAX_CHARS`（12000）、`PA_AGENT_IDENTICAL_CALL_LIMIT`（3）。工具超时使用可中断的主线程，AKShare 使用可终止的子进程；不支持把有副作用工具放入不可终止的后台线程。

行情向模型提供前还须配置明确的 AI 数据处理来源（研究工具为 `akshare:research`），展示准入不会自动授予模型处理权。记忆按每类来源 100 条分批建立全历史索引，已提交批次在后续查询继续使用；首次大批历史未完成时返回 index_complete=false，不能声称没有旧记录。无向量服务时返回关键词模式说明。监察表写入接受明确的完整用户命令，例如 `将 510300 加入监察表，目标价 9，优先级 3`，只读任务不可写。

Trace 默认只保存脱敏参数与结构摘要，30 天后由 scheduler 清理；不存模型思维链。账户删除清理新增私有表。Admin 采用现有命名令牌与审计，公网 OIDC/MFA 仍是后续工作。

本轮迁移链：`a03_trading_discipline` → `3a3b31208da1`（工具轨迹、监察表、记忆）→ `3fbee3f16914`（仓位分类、布局版本）→ `9c20a1324c8d`（净值对比、成本来源与消息图表）。测试库 `alembic check` 通过；远程测试服务器已完成本轮 API 与迁移部署；远程模拟器及上游结果见 [部署记录](../deploy/beta/deployment-2026-10-28.md)。

## 新产品范围的验证

模拟器用 `SIMNAV` 合成基金验证：成本留空按 1.05 的净值保存、记录来源时间，持仓显示较上一净值上涨 5%，并明确说明非实时；监察表添加、图表查看与移除通过。合成图表是测试数据；真实 AKShare 历史行情和 DeepSeek 搜索的本机样例结果另见 [上游验证](upstream-verification-2026-10-27.md)。

联网搜索使用新的 AI 披露版本 2；搜索词发送到 DeepSeek Search，工具轨迹中的搜索词脱敏，并共享每日预算。旧版授权不会自动扩展到搜索。部署时若固定了 `PA_AI_DISCLOSURE_VERSION=1`，须更新披露并由用户重新授权；旧版配置拒绝搜索。搜索结果仅可通过登记的来源引用，未经登记的模型链接被移除。系统提示要求仅复盘事实，不提供投资决策，但不能仅凭提示词保证模型输出始终符合产品要求，仍需真实输出评测。
