# 复盘了 · Portfolio Agent

> 个人投资组合复盘应用：iOS 客户端 + 自托管后端。记录持仓与交易、计算估值、生成日/周复盘报告，并提供有界的 AI 复盘助手。
> Portfolio journal with an iOS client and a self-hosted backend: positions, valuation, scheduled recaps, and a bounded ReAct AI assistant.

本项目以开源方式继续维护，**不提供商业订阅或付费功能**，所有功能免费。代码按现状提供，不构成投资建议。

## 功能

- **持仓与账本**：手工账户、初始快照、买卖交易；移动平均成本、已实现/未实现盈亏分离；幂等与版本冲突保护；PostgreSQL 行级安全（RLS）多租户隔离。
- **估值**：A 股 / ETF / 港股 / 美股 / 场外基金；实时/延迟/收盘/净值四种行情口径；收盘后按真实数据源时间展示最近收盘价（`close_mark`，绝不冒充实时）；缺行情或汇率时如实标记，不用成本或 0 填充。
- **复盘报告**：每天两次（A/港股收盘后 16:30 Asia/Shanghai、美股收盘后 16:30 America/New_York）与周六 09:00 Asia/Shanghai 周报；按用户 id 升序分批次投递。两阶段管线：先批量生成无 AI 的事实基线，再对合格用户批量生成 AI 摘要（任务内线程并发、可配置抽样比例）。
- **Ask Agent**：围绕组合或某份报告的追问；有界 ReAct（轮数/时间/token 上限）、只读工具、发送前复核授权、引用溯源；SSE 推送状态。
- **行情采集**：常驻 AKShare 取数进程池（akshare 只在 worker 内 import），行情/新闻/汇率三独立节奏，收盘时段自动降频；跨用户共享缓存。
- **管理后台**：Vue 3 + Vite + Naive UI 静态前端（运行概览/报告查看/触发报告/任务状态/AI 调用轨迹），复用 Admin API（Bearer 管理员令牌 + 审计原因）。
- **隐私**：AI 授权与撤回、账户删除（立即撤销登录并后台擦除私有数据）、轨迹保留期与脱敏选项。

## 架构

```
iOS App ──HTTPS──> Caddy（api 域名：/api/mobile/v1 · /health；admin 域名：Vue 静态页 + /api/admin/v1 反代）
                    │
              FastAPI api（uvicorn，鉴权 + RLS 租户隔离）
                    │
     ┌──────────────┼──────────────────────────┐
     │              │                          │
 PostgreSQL      Redis ×3                 异步任务链
 （唯一事实源）   broker（Celery 队列）      Outbox → dispatcher → broker → worker / worker-reports
                 cache（行情共享缓存）      scheduler（两次日报 + 周六周报）
                 auth-cache（令牌/限流）     collector（行情/新闻/汇率，常驻取数池）
```

- API 只做同步读写与任务投递；耗时任务走 Outbox → Celery，租约 + fencing 保证不重复执行。
- 报告管线两阶段：`report_facts` 批量事实（无 AI）→ `report_ai` 批量摘要（线程并发、抽样门槛）。
- 管理 API 独立身份、角色与审计；前端为纯静态文件，由 Caddy 直接托管。

## 快速开始（本地）

前置：Docker、Python 3.12+、[uv](https://github.com/astral-sh/uv)。Node 20+ 仅在修改管理后台时需要。

```sh
# 1) 基础服务 + 迁移 + 本地样例数据
docker compose up -d --wait
uv sync --locked --extra market-data
export PA_MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:local-owner-only@127.0.0.1:55432/portfolio
uv run alembic upgrade head
uv run portfolio-seed

# 2) 启动各进程（各开一个终端）
uv run portfolio-api                 # http://127.0.0.1:8000/docs
uv run celery -A portfolio_backend.worker:celery worker -Q interactive_ai,privacy --concurrency=2
uv run celery -A portfolio_backend.worker:celery worker -Q reports,daily_reports,weekly_reports --concurrency=2
uv run portfolio-dispatcher
uv run portfolio-scheduler
uv run --extra market-data portfolio-data   # 可选：行情采集

# 3) 测试与静态检查
make test
make check
```

配置项见 [`.env.example`](.env.example) 与 `src/portfolio_backend/config.py`（`PA_` 前缀）。敏感值放 `.env` 或 `secrets/`，不要提交。

## 文档

- [运行时序图](docs/sequence-diagrams.md)：各模块链路（登录、首页、持仓、报告管线、Ask Agent、采集、管理后台、账户删除）
- [自托管部署](deploy/beta/README.md)
- [行情采集优化](docs/collector-p0.md) · [报告压测基线](docs/loadtest-reports.md)
- [数据源与合规](docs/providers.md) · [低成本数据接入](docs/low-cost-data.md) · [手机号认证（可选）](docs/phone-auth-aliyun.md)
- [管理后台（Vue）](docs/admin-vue.md) · [OpenAPI](docs/openapi.json)

## 许可证

[MIT](LICENSE)。第三方依赖与数据源各有自己的许可；商业使用数据源前请自行确认授权（见 `docs/providers.md`）。
