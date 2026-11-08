# Collector P0 优化记录（批量取数 / 常驻进程 / 收盘降频）

> 对应调研报告 §4.1。目标：collector 空载 CPU 109% → 10–20%，内存 175MiB → 100–120MiB 量级，
> 2C/1.6G 测试服在不增加容器限额的前提下支撑 100 测试用户。

## 1. 改了什么

| 文件 | 变更 |
|---|---|
| `src/portfolio_backend/providers/market_worker.py`（新增） | 常驻取数进程池：akshare 只在 worker 内 import 一次；崩溃/超时自动重建重试一次，最终失败抛 `DomainError("market_source_unavailable")`；空闲 120s 自动回收（保护 API 进程内存）；`spawn` 启动方式避免在带线程的 API 进程里 fork |
| `src/portfolio_backend/providers/akshare_worker.py` | 抽出 `fetch_rows(feed, symbol)`（原 `main()` 逻辑不变），CLI 子进程入口保留，作为回滚与手工探测通道；新增 `warmup()` |
| `src/portfolio_backend/providers/market.py` | `_fetch` 改走常驻池（池不可用时自动回退到原来的单次子进程路径）；新增 `market_session()` 判断 A 股开闭市；`_subprocess_fetch` 保留原实现 |
| `src/portfolio_backend/collector.py` | `tick()` 只做行情（新闻/汇率移出）；新增 `collect_news()`（每新闻周期全量跑一遍有权益的活跃持仓）；`main()` 拆成行情/新闻/汇率三个独立节奏；新增 `effective_quote_poll()` / `effective_news_poll()` |
| `src/portfolio_backend/config.py` | 新增 `off_hours_quote_poll_seconds=600`、`off_hours_news_poll_seconds=900`、`data_fetch_workers=2`；`data_max_instruments_per_tick` 默认 20 → 200 |
| `tests/test_collector_p0.py`（新增） | 纯单元测试：开闭市判定、有效轮询间隔、批量表按 symbol 过滤、进程池失败 → DomainError 映射、池回退子进程 |
| `tests/test_public_data.py` | 原「tick 内收新闻」断言迁移到 `collect_news()`（权益过滤断言不变） |

未动：`api.py`、`worker.py`、`scheduler.py`、`services/jobs.py`、`services/catalog.py`、`deploy/**`、compose、Dockerfile、Redis 键设计。

## 2. 批量数据源与时间戳验证结果

验证方式：venv 内 akshare 1.19.3 源码逐字段核对（`inspect.getsource`）+ 本机实测一次真实接口
（eastmoney 在本机网络不稳，`stock_zh_a_spot_em` 未能取到实盘数据，但字段列表来自源码，结论确定）。

| 源接口 | 覆盖 feed | 每行时间戳 | 结论 |
|---|---|---|---|
| `fund_etf_spot_em()` | `akshare:etf` | `更新时间`（f124，UTC 秒 → Asia/Shanghai，每行独立） | ✅ 已批量（原有），每轮询窗口一次全表，cache key 为 provider 级 |
| `stock_hk_spot()` | `akshare:hk-spot` | `日期时间`（如 `2026/11/06 16:08:53`，每行独立） | ✅ 已批量（原有），实测 2803 行 |
| `stock_zh_a_spot_em()` | `cn-minute` / `sina-minute` | **无**：请求字段列表不含 f124（更新时间），返回列只有 序号/代码/名称/最新价/涨跌幅/… 年初至今涨跌幅 | ❌ **不用作批量**。无逐行时间戳时用快照会违反「不伪造 as_of」约束，因此 A 股个股保持逐标的分时取数（经常驻池） |
| `stock_us_spot_em()` | `us-minute` | **无**（同样不含 f124） | ❌ 保持逐标的 `stock_us_hist_min_em` |
| `stock_zh_a_spot()`（新浪） | — | 有 `时间戳` 列，但 docstring 明示「重复运行会被新浪暂时封 IP」 | ⚠️ 备选（见后续建议），本期未采用 |

因此批量收益集中在 ETF/HK 全表：无论 tick 内出现多少只 ETF/HK 标的，同窗口内上游只打 1 次；
`normalize()` 按 symbol 过滤（ETF/HK），`publish_mark` 的 `where as_of <= mark.as_of` 保证旧行不覆盖新行。

## 3. 常驻进程池生命周期

- 首次取数时惰性创建 `ProcessPoolExecutor`（`spawn`，worker 数 = `PA_DATA_FETCH_WORKERS`，默认 2，允许 1–8）。
- akshare 在每个 worker 第一次执行任务时 import 一次（空闲 worker 保持轻量 ~15MiB，不预热）。
- 崩溃 / 超时：丢弃整个池 → 重建 → 重试一次；再次失败抛 `market_source_unavailable`（503），与旧路径语义一致。
- 空闲回收：120s 无取数由守护线程关闭池。collector 交易时段 120s 一 tick，池保持常驻；
  收盘后 tick 间隔 600s，池随开随建（每 10 分钟付一次 ~1–2s import，可忽略）。
  API 仅在开仓补价时偶发取数，用完 120s 后自动释放，不会常驻占用 api 容器内存。
- 池不可用（如平台不支持 spawn）时 `_fetch` 自动回退单次子进程，回退不影响现有错误码。

## 4. 收盘降频与节奏解耦

- `market_session()`：周一至周五 09:30–11:30、13:00–15:00（Asia/Shanghai）为开市，其余（含午休/夜间/周末）闭市。
  不引入节假日日历：节假日按闭市节奏轮询，只是多打几次低频请求，不产生错误数据。
- 有效节奏：行情 = 开市 `PA_QUOTE_POLL_SECONDS`（120）否则 `PA_OFF_HOURS_QUOTE_POLL_SECONDS`（600）；
  新闻 = 开市 `PA_NEWS_POLL_SECONDS`（300）否则 `PA_OFF_HOURS_NEWS_POLL_SECONDS`（900）。
- `main()` 三个独立时钟：行情 tick（cursor 公平轮转，上限 200/次）、新闻 pass（全量权益持仓）、
  汇率 `refresh_fx`（内部 `fx-fetched` 缓存锁继续生效，`PA_FX_POLL_SECONDS=21600`）；目录同步不变。
  循环每 30s 唤醒一次检查目录与到期节奏；`--once` 仍为「目录 + 行情 + 新闻 + 汇率」全量一遍。
- 收盘后（含夜间）：每 10 分钟 1 次行情（批量源 1–2 个上游请求 + A 股逐标的受限配额）、
  每 15 分钟 1 次新闻、汇率 6h 1 次 → 夜间 CPU 从 ~110% 降到接近空闲。

## 5. 运维旋钮（env）

| 变量 | 默认 | 范围 | 说明 |
|---|---|---|---|
| `PA_QUOTE_POLL_SECONDS` | 120 | 60–3600 | 开市行情轮询/缓存 TTL |
| `PA_OFF_HOURS_QUOTE_POLL_SECONDS` | 600 | 60–3600 | 闭市行情轮询 |
| `PA_NEWS_POLL_SECONDS` | 300 | 120–86400 | 开市新闻轮询 |
| `PA_OFF_HOURS_NEWS_POLL_SECONDS` | 900 | 120–86400 | 闭市新闻轮询 |
| `PA_FX_POLL_SECONDS` | 21600 | 1800–86400 | 央行中间价刷新（不变） |
| `PA_DATA_MAX_INSTRUMENTS_PER_TICK` | 200 | 1–500 | 单 tick 行情配额（beta 部署文件内仍为 5，由部署侧决定是否上调） |
| `PA_DATA_FETCH_WORKERS` | 2 | 1–8 | 常驻取数进程数；串行采集只用 1 个，多余容量服务 API 并发补价与崩溃接管 |

## 6. 预期资源影响

- CPU：批量源把「每标的 1 个 import akshare 的子进程」降为「每窗口 1–2 个常驻 worker 调用」；
  A 股逐标的请求也复用已 import 的 worker，省掉 1–3s/次的重复 import → 预期 10–20%。
- 内存：collector = 主进程（~60MiB）+ 1 个 akshare worker（~120MiB）+ 空闲 worker（~15MiB/个）。
  默认 2 workers 约 190–210MiB；`PA_DATA_FETCH_WORKERS=1` 约 175MiB。100–120MiB 的报告中位数
  需要 Linux 实机 RSS 复核；如需压内存，优先调成 1 并让 API 侧回退路径工作（已实现）。
- 若实测仍超预算：把 `data_fetch_workers` 调到 1；或按 §7 引入新浪批量快照。

## 7. 回滚与风险

- **回滚**：`git revert` 本改动即可；`akshare_worker.py` CLI 入口与 `_subprocess_fetch` 原实现均保留，
  旧行为（每标的起子进程）无需代码即可由回退路径接管。
- Redis 键与用法未变：`public-data:v1:*`、`public-data:instrument-cursor`、`public-data:fx-fetched` 原样。
- 风险 1：spawn 在个别受限容器环境不可用 → 已自动回退子进程路径，只有性能收益消失，无正确性影响。
- 风险 2：A 股逐标的请求仍是覆盖瓶颈（每 tick ≤200 只、串行）；500 只 A 股全量覆盖需要数分钟，
  交易时段 120s 新鲜度对纯 A 股重仓组合只能近似。见后续建议。
- 风险 3：`collect_news` 每次全量遍历有权益持仓，搜索类上游调用由既有 `ExternalBudget`
  （wallstreetcn 1000/日、deepseek 100/日）预算兜底，不会放大计费。

## 8. 后续建议

1. A 股批量：评估 `stock_zh_a_spot()`（新浪，含 `时间戳`）作为 `cn-minute`/`sina-minute` 的批量快照源，
   需先解决「重复运行封 IP」与时间戳格式问题；或给 eastmoney `clist/get` 加 `f124` 字段的自有轻量请求。
2. tick 内并行：将同 provider 组按 `cache_key` 去重后并发提交常驻池（worker 数才真正发挥作用），
   可显著提升 A 股覆盖速度。
3. 部署侧：把 beta 的 `PA_DATA_MAX_INSTRUMENTS_PER_TICK` 由 5 调到 ≥200，并按实测调整 `PA_DATA_FETCH_WORKERS`。
4. 上机复测 `docker stats`，验证 109% → 10–20% 与内存目标；必要时为 collector 容器调整 mem_limit。
