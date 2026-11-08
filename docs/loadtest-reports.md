# 日报/周报压测：100 用户在 2C/1.6G 轻量服务器上的运行基线

> 目标：验证 [方案 A 两阶段报告管线](sequence-diagrams.md) 在 2 核 / 1.6GB 测试服务器上能否在投递窗口内完成 100 用户的日报（每日两次）与周报（周六一次）。
> 工具：`scripts/loadtest_reports.py`（真实 scheduler / materialiser / worker 代码 + 可替换的 AI stub）。
> 测量日期：2026-11-08。

## 1. 测试设计

### 1.1 场景

| 会话 | 触发窗口 | 投递顺序 | 报告期 |
|---|---|---|---|
| `daily_cn`（A/港股收盘） | 每周 16:30 Asia/Shanghai | user id 升序，每 `PA_REPORT_DISPATCH_BATCH_USERS`（默认 50）户延迟 `PA_REPORT_DISPATCH_BATCH_INTERVAL_SECONDS`（默认 15s） | 上海日期 |
| `daily_us`（美股收盘） | 每周 16:30 America/New_York（北京次日 05:30/04:30） | 同上 | 纽约日期 |
| `weekly` | 每周六 09:00 Asia/Shanghai | 同上 | 上海日期 |

每个用户 5 个持仓；`daily_report=true`、`weekly_report=true`；两阶段管线：`report_facts`（批量事实，无 AI）→ `report_ai`（批量摘要，任务内 4 线程并发，默认 8 户/任务）。

### 1.2 方法

1. 在已迁移的空库中 `seed --users 100 --positions 5`：100 用户 × 5 持仓、每个持仓有新鲜行情（fixture 源），`complete=true`。
2. `run --session cn|us --workers 2 --ai-delay N`：
   - 调用真实 `scheduler.tick(at=会话窗口+5min)` 写入 Outbox（验证分批 due_at）；
   - 循环执行「materialise Outbox → claim/执行 Celery 任务体」直到队列排空；
   - AI 用 stub gateway 替代（`ai-delay` 模拟单次模型耗时，stub 仍跑在真实的 `report_ai` 线程池里，可观测最大并发）；
   - 统计：调度耗时、首份报告出现时间、总排空时间、AI 调用数/最大并发、是否超时。
3. 本地（Mac arm64，Docker）用于验证逻辑与并发；2C/1.6G 测试服务器用于容量基线。

## 2. 本地结果（100 用户）

| 会话 | 调度 | 事实阶段 | AI 摘要（stub 延迟） | 最大并发 | 总排空 |
|---|---|---|---|---|---|
| daily_cn（周六，同时含周报 100 户） | 0.13s | 首份报告 0.76s | 200 次 × 0s | 1（0 延迟瞬时） | **1.3s** |
| daily_us | 0.08s | 首份报告 0.68s | 100 次 × 5s | **8** | **66.3s** |

US 会话 100 份摘要按 5s/份、8 并发 → 理论 62.5s，实测 66.3s，管线开销 <6%。事实阶段（快照 + 落库）100 户 <1s。

## 3. 2C/1.6G 测试服结果（2026-11-08，100 用户 × 5 持仓）

测试服实际配置：`worker` 并发 1、`worker-reports` 并发 1（内存受限下调低，compose.env 可调）；`report_ai` 任务内 4 线程。
AI 用 stub 延迟 2s/份运行在真实线程池内，外推列按真实 DeepSeek 45s/份估算。

| 会话 | 执行并发 | 调度耗时 | 首份报告 | AI 调用 | 最大并发 | 实测总排空（stub 2s） | 外推（真实 45s/份） |
|---|---|---|---|---|---|---|---|
| daily_cn | 2 个任务并行 | 0.23s | 1.77s | 100 | 8 | **28.6s** | ~9.4 分钟 |
| daily_us | 2 个任务并行 | 0.28s | 1.95s | 100 | 8 | **28.9s** | ~9.4 分钟 |
| weekly | 2 个任务并行 | 0.33s | 1.98s | 100 | 8 | **28.9s** | ~9.4 分钟 |
| daily_cn（**当前部署配置**） | 1 个任务并行 | 0.29s | 1.81s | 100 | 4 | **52.9s** | ~18.8 分钟 |

- **事实阶段**：100 户的首份报告在 1.8–2.0 秒内落库；调度（写 100 条带 due_at 的 Outbox）耗时 ≤0.33s。
- **内存**：压测期间宿主机可用内存稳定在 **363–430MiB**（压测进程本身仅 36–70MiB），全程无 OOM、无 swap 抖动；worker 并发降到 1 后空闲可用内存回升到 ~440–570MiB。
- **真实环境验证**：部署新版调度器后，当天 17:37（北京时间）自动补跑 `daily_cn`（catch-up），两阶段完成且 `analysis=ai` —— 真实 DeepSeek 摘要链路正常。
- **结论**：2C/1.6G 测试服支撑 100 用户的「每日两次日报 + 周六周报」没有压力：一次会话的 AI 排空约 9–19 分钟，远小于投递窗口；两个日报会话按时段天然错开（A/港股傍晚、美股次日凌晨）。真正的约束是外部模型延迟/并发，而不是本机 CPU/内存。若放宽 `worker-reports` 并发到 2，可把单会话 AI 排空时间减半。

> 注：本机内存只有 1.6GB，日常请保持 worker 并发 1；10k 用户场景需按 [AI 容量分析] 的结论做 AI 摘要分级与 worker 池化。

## 4. 容量结论与 AI 外推

- **事实阶段不是瓶颈**：100 户 <1s（含 500 次持仓读取与 100 次报告落库）。10k 户按线性估算 <2min（批任务 500 户/个、20 个任务）。
- **AI 摘要是主耗时**，且与外部模型延迟成正比。真实 DeepSeek flash 摘要约 30–60s/份：
  - 默认 2 个 reports worker × (`report_ai_batch_size=8`, `report_ai_concurrency=4`) = **8 份并发**；
  - 100 户单会话 ≈ 100 × 45s / 8 ≈ **9–10 分钟**；若两会话错开（A/H股傍晚、美股次日凌晨）互不叠加；
  - 1000 户单会话 ≈ 1.5–2 小时，需调大 `report_ai` 并发或缩短模型耗时；
  - 10k 户必须启用 `PA_REPORT_AI_SAMPLE_RATIO`（0.1–0.2）或增加 reports worker 实例数，否则单会话排空时间会超过投递窗口。
- **内存**：两阶段均为 IO-bound，worker 进程常驻 ~100–230MiB（加载 langchain 后）；stub 延迟测试下容器内存平稳（见 §3）。
- **结论**：2C/1.6G 支撑 100 用户日报（两次）与周报没有压力；真正的约束是外部 AI 延迟与 provider 并发，而不是本机 CPU/内存。

## 5. 复现命令

```sh
# 0) 空库 + 迁移（在任意可迁移数据库上；服务器上建议独立 scratch 库）
# 1) 种子数据
python scripts/loadtest_reports.py seed --users 100 --positions 5 --clear
# 2) 单会话压测（--ai-delay 模拟模型耗时，单位秒）
python scripts/loadtest_reports.py run --session cn --ai-delay 0
python scripts/loadtest_reports.py run --session us --ai-delay 45 --workers 2
# 3) 周报
python scripts/loadtest_reports.py run --session weekly --ai-delay 45
```

注意：`run` 默认使用「昨日窗口 +5 分钟」以便所有分批 due_at 均已到期；`--at` 可指定会话时间。
