# 免费源优先的实现（2026-10-24）

本页取代同日早期“先采购企业套餐”的接入顺序。按用户最新决定，行情使用 AKShare；新闻优先华尔街见闻官方 skills 中的免费接口，DeepSeek Search 兜底。原有“没有实时价格不能录入”继续生效。没有把免费接口开放理解为已确认付费 App 的商业数据授权。

## 已实现链路

独立 `portfolio-data` 进程采集，移动 API 只读 PostgreSQL，用户刷新页面不直接打上游。

- AKShare 固定到 lockfile 中的 1.19.3，作为可选 `market-data` 依赖；子进程隔离，50 秒硬超时，避免库中无 timeout 的请求卡住 API。
- A 股新浪 1 分钟数据、A 股/ETF 新浪目录和报价；沪深 ETF 也可用东财批量数据；港股新浪现货快照明确标记 `delayed`，不冒充实时价。没有场外基金净值或泛黄金价格替代。
- 价格取未复权的最新成交/分钟收盘价；使用数据自带的完整日期时间。`fetched_at` 不冒充行情时间，NaN、负数、缺时间、未来时间拒绝；旧数据继续显示过期，不能刷新有效期伪装实时。A 股使用分钟更新，不宣传逐笔行情。
- 同一 ETF 行情批次跨标的共享 120 秒；个股以提供方/代码共享 120 秒。Redis 锁合并并发请求；故障冷却 60 秒。进程或 Redis 故障时不把失败当成功数据。
- WSCN 全局最新新闻缓存 5 分钟，按标的免费搜索缓存 1 小时；没有新鲜相关结果时才用 DeepSeek Search，结果缓存 6 小时。冷缓存锁被其他进程持有时不抢跑付费兜底。
- 新闻只保留标题、短摘录、来源、时间、原文链接；不存正文、不获取付费全文。WSCN 摘录最长 50 字，DeepSeek Search 使用 `need_content=false`、最多 5 条引用，摘录最多 200 字；这不是对任何文章的自动再分发授权。
- 新闻按 URL（去跟踪参数和 fragment）全局去重，和 instrument 建关联，不按用户复制新闻。只为活跃用户持有的标的收集；匹配只依据公开标的名称/代码，不发出数量、成本、账户、持仓权重或用户提问。
- 返回首页 `news` 前检查用户实际持仓，避免跨用户内容关联泄露。iOS 在复盘框显示来源/摘要并链接原文，不添加独立新闻主入口。本轮未实现 APNs 系统推送。
- 内容只保留新近 48 小时且发布时间明确的引用，数据库最多再存 24 小时，采集轮次清理过期引用。无时间或无匹配不编造新闻，也不凭关键词命中断言导致股价涨跌。

## 成本阀门

`PA_SEARCH_DAILY_LIMIT=100`：本部署共享的 DeepSeek Search 每自然日最多预约 100 次，按北京时间换日；每次请求前先在 PostgreSQL 原子占额。失败、超时和空结果不返还本地预约，保证预算上界。Redis 重启不会抹掉当日已预约次数。`0` 完全关闭付费搜索。

按[官方 Search 价格](https://api-docs.deepseek.com/zh-cn/guides/search/) ¥5/1,000 次成功请求，上限 100 次约 **¥0.50/天、30 天 ¥15**，不包含聊天/日报模型 token 和服务器。用户增长不会自动抬高此上限；达到上限就只提供已有免费内容，不暗中继续扣费。共享同一 DeepSeek 账户的其他应用不受此部署预算控制。

免费搜索也有全站每天 1,000 次预约上限。每轮最多处理 20 个持仓标的，30 秒进入下一轮，稳定游标避免某些失败标的永远占据队首。这里的“120 秒”是缓存 TTL，不承诺任意数量标的都能每 120 秒完成采集；源端耗时、批大小和错误会影响覆盖，届时用负载及新鲜度指标调整。尚未通过 1 万 DAU 压测。

## 如何运行

```sh
uv sync --locked --extra market-data
make migrate
# 只同步一次目录（沪深股票、ETF、港股）
uv run --extra market-data portfolio-catalog
# 持续运行：先同步一次目录，再采集持仓行情和新闻
PA_DATA_COLLECTION_ENABLED=true uv run --extra market-data portfolio-data
```

移动端不再需要调用 Admin instrument API 逐个预装标的。采集进程启动时从 AKShare 公开列表建立目录（默认每 24 小时刷新），`GET /api/mobile/v1/instruments?query=` 直接在 PostgreSQL 中按代码或名称搜索：

| kind/provider | 覆盖 | 报价类型 |
|---|---|---|
| `cn-stock` / `akshare:sina-minute` | 沪深京 A 股，代码/名称来自新浪现货列表 | 新浪未复权 1 分钟，带源时间 |
| `cn-etf` / `akshare:sina-minute` | 沪深 ETF，代码/名称来自新浪 ETF 列表 | 新浪未复权 1 分钟，带源时间 |
| `hk-stock` / `akshare:hk-spot` | 港股，五位代码 | 新浪港股现货快照，明确标记 `delayed` |

`akshare:cn-minute`、`akshare:etf`、`akshare:hk-minute`、`akshare:us-minute` 仍可由后台手工配置使用，但不再要求预装目录。美股和场外基金尚未自动导入：前者在测试网络抓取超时，后者是净值而不是实时价。

目录只代表“可搜索/可记录”，不代表报价一定存在或当前。开仓快照只要标的已启用且展示权已确认即可录入；缺失、过期、延迟、收盘或净值报价按真实时间和类型显示，估值仍只使用当前有效报价。生产采集写行情还需 `PA_COMMERCIAL_DATA_PROVIDERS` 包含对应 provider，或由 `PA_AKSHARE_DISPLAY_CONFIRMED=true` 表示本部署确认 AKShare 展示；这不是法律结论。生产新闻展示还需 `PA_NEWS_DISPLAY_PROVIDERS`。AI 外发数据另受 `PA_AI_MARKET_DATA_PROVIDERS` 控制。

采集进程只刷新仍有持仓的标的，不按整个搜索目录轮询。FX 实采、交易日历与盘后报价有效期规则尚待实现；不拿今日拉取动作给周末旧报价续命，也不把旧汇率当实时汇率。

## 实测与待验收

2026-10-24 本机：华尔街见闻最新/搜索接口 HTTP 200，适配解析成功；DeepSeek Search 成功返回 5 条带时间引用。AKShare 东财 A 股分钟和 ETF 请求被上游断开连接，适配器正确返回 unavailable。AKShare 新浪分钟接口成功返回 `sh600519` 在 2026-10-23 15:00（北京时间）的收盘数据，系统正确判定在本次周末测试时已过期，没有把拉取时间当成报价时间。不可把单次成功或失败推断为国内云上稳定性。

尚无阿里云实例。用 [网络探针](../scripts/probe-network.py) 在真实华东实例验证 `api.deepseek.com`、`api-one-wscn.awtmt.com` 及实际 AKShare 上游域名，再测 API 语义、工作日开盘、限流与重连。探针仅检测 DNS/TCP/TLS，不等于行情或授权验收。

## 授权证据

- [AKShare 数据说明](https://akshare.akfamily.xyz/data_tips.html)讨论数据质量风险；[项目概览](https://akshare.akfamily.xyz/introduction.html)和仓库 Statement 明确其接口/数据用于学术研究。MIT 代码许可与交易所/上游数据权利分别确认；当前不能声称已确保商业不侵权。
- [AKShare 股票文档](https://akshare.akfamily.xyz/data/stock/stock.html)明确港股及相关港美股分钟接口存在延迟。免费源与全市场实时目标存在覆盖缺口，不自动降低用户此前的录入要求。
- [华尔街见闻官方技能](https://github.com/wallstreetinsights/skills/blob/main/wallstreetcn-news.md)公开 API、品牌和原文引用方式；[MIT LICENSE](https://github.com/wallstreetinsights/skills/blob/main/LICENSE)覆盖软件/文档，没有在仓库中找到所有新闻内容在收费 App 持续分发的完整条款。接入时保留来源，商业发布条件仍未确认。
- [DeepSeek Search 文档](https://api-docs.deepseek.com/zh-cn/guides/search/)提供接口与收费依据；购买搜索调用不自动扩大原网页权利。本轮不启用 Fetch。

未购买套餐、联系供应商、代表用户签约或将这些源标记为已商用授权。技术开发继续采用用户选定的免费优先方案。
