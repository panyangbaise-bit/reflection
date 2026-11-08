# 运行与验收

## 进程与权限

API、Worker、Dispatcher 和 Scheduler 分别启动。API 启动校验数据库角色恰为 `pa_app` / `pa_control`，禁止 superuser 或 BYPASSRLS。数据库凭据、供应商密钥与 Apple 文件在部署平台配置，不提交 Git。

`pa_app` 执行用户查询并使用事务局部 `app.user_id`；`pa_control` 用于认证、可信任务和管理操作。它能跨用户访问，因此只能用于内部代码路径。用户输入不得成为任意 SQL 或控制面查询条件以外的可执行内容。后续可继续拆为 auth/worker/admin 独立角色。

数据库连接预算：每个 API 进程的用户池 10+5、控制池 5+5。Worker 当前每任务建立/释放自己的池；生产迁移为每子进程复用池前，先测量连接创建/销毁成本，按并发总量配置 PostgreSQL 限额。API 默认仅监听 loopback；部署在 HTTPS 反向代理后，限制请求体、连接和匿名速率。

Redis 缓存与 Broker 分开；Broker `noeviction` + AOF。业务状态从 PostgreSQL 恢复。当前只把 Redis 用于认证限流和 Broker，估值没有使用进程内缓存作为事实源。

## 队列与排障

`interactive_ai`、`daily_reports`、`weekly_reports`、`privacy` 四队列已接通。队列载荷只有 job ID；用户与业务参数从数据库恢复。Worker 成功提交同时更新结果及任务状态，fencing 拒绝过期尝试的迟到结果。

Dispatcher 每 2 秒扫描 Outbox、过期租约与待投任务；已投但未认领的任务 60 秒后可重投。`jobs` 数据不能依赖 Celery result backend。失败终止后要创建新的可审计业务操作，当前后台没有无边界“重跑全部”按钮。

API `/health/live` 检查进程，`/health/ready` 检查数据库和缓存。当前尚未接通集中指标/告警、APNs 或外部供应商 SLO，不能把存活检查当成全业务健康检查。

## 容量验收（尚未完成）

依照设计方案，以 5 万注册用户、100 万持仓、1 万日活作为数据规模。先合成用户，再为每个并发虚拟用户准备不同的真实应用会话，写入忽略提交的 `secrets/load-users.json` 数组：`access_token` 与可选 `position_id`。必须是独立测试环境中的合成资产；带 position_id 会持续写入买入记录。

```sh
PA_LOAD_USERS=secrets/load-users.json uv run locust -f scripts/locustfile.py \
  --host http://127.0.0.1:8000 --users 200 --spawn-rate 20 --run-time 10m --headless
```

用户数不等于 RPS。逐步调整并发与等待时间，分别达到 200 RPS 30 分钟、500 RPS 5 分钟；同时测 500 个行情页面、1 万/5 万报告和 AI 隔离队列。访问凭据 15 分钟过期，长测试需要准备轮换客户端，不能把过期 401 当作容量失败。

记录 API p95、错误率、DB 连接/锁/IO、最老任务年龄、供应商 TPM/RPM、报告延迟与单次成本。目标：读 p95 <300ms、写 <500ms；压测结果不能替代真实供应商/市场覆盖与配额。当前脚本只是复现负载入口，没有取得目标容量结论。

## 数据迁移与恢复

Alembic 是部署步骤；代码发布前执行迁移，失败即停止。现有两版迁移在独立测试数据库从空库升级，并检查 ORM/schema 无差异。生产迁移遵循 expand/backfill/switch，不能用当前初始迁移覆盖既有业务库。

本次没有读取/导入旧 SQLite，也没有停止原服务。历史迁移需要专用导入工具、来源/成本语义核对、预览与重复执行测试。

PostgreSQL WAL/PITR、异地备份、RPO/RTO 演练和删除墓碑重放尚未接入。开发 Compose 卷不是生产备份方案。
