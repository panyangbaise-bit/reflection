# 外部服务边界

## DeepSeek

默认服务商为 `deepseek`。配置 `PA_DEEPSEEK_API_KEY` 后直连固定官方地址 `https://api.deepseek.com/chat/completions`，默认模型 `deepseek-flash`，可用 `PA_DEEPSEEK_MODEL` 调整。密钥仅放服务端 Secret 或被 Git 忽略的本地环境文件，不重定向或自动切换供应商。

输入有字符上限，超限不静默截断持仓；默认输出上限 2048 token，非思考模式。保存最终回答及实际输入/输出/缓存 token，不保存推理链。余额、鉴权、请求和截断错误不盲重试；限流、5xx、网络错误由任务框架有限重试。未完整生成的回答不扣除免费追问。

自动报告使用独立 `scheduled_ai` 授权，外发前及提交时复核。未授权或供应商失败时交付事实报告，原因记录在快照 `analysis` 元数据。行情及衍生估值外发需配置 `PA_AI_MARKET_DATA_PROVIDERS`，默认空；所有报价与 FX 来源均需确认模型用途。详见[采购核查](data-sources-2026-10-24.md)。

## 兼容 AI 网关

`PA_AI_GATEWAY_URL` 是部署方固定配置的受信任供应商适配层，不能由移动端指定。当前仓库只实现 HTTP 客户端，不包含一个虚构可用的模型服务；空配置返回 `ai_unavailable`。生产必须使用 HTTPS。外发前校验 `interactive_ai` 的服务商及披露版本，并在最终提交时再次验证用户状态与授权。

请求：`POST PA_AI_GATEWAY_URL`，`Authorization: Bearer PA_AI_GATEWAY_KEY`。示例结构：

```json
{
  "request_id": "服务端运行UUID",
  "purpose": "interactive_ai",
  "provider": "已披露的供应商标识",
  "context": {"positions": [], "complete": true},
  "messages": [{"role": "user", "content": "我的持仓是否过于集中？"}],
  "instructions": "解释给定持仓与复盘事实……"
}
```

响应：

```json
{"text":"回答正文","model":"实际模型标识","input_tokens":100,"output_tokens":40}
```

上下文剔除用户 ID、账户 ID、组合 ID、凭据、设备 token。对话正文是用户输入，仍可能包含用户自行输入的个人信息；供应商和披露不能声称绝不接触任何个人数据。账本数值来自确定性代码，模型文字不写回账本。网络/解析错误不会伪装成成功回答。

超时：连接 5 秒、HTTP 读超时 90 秒、Worker 硬期限 120 秒、DB 租约 180 秒、Redis visibility timeout 300 秒。分层上界不同，HTTP 读超时不是整个模型推理的严格总时长保证；真正的进程总时长由 Worker 约束。网关建议按 request_id 实现幂等与成本审计。

## Apple

登录使用官方 JWKS、RS256、issuer/audience/expiry/nonce；一次性挑战只在验证成功后消费。不会信任客户端提交的 user_id 或邮箱。

## 行情

目录默认由采集进程从 AKShare 公开列表自动同步，移动端搜索只读 PostgreSQL；`POST /api/admin/v1/catalog/sync` 可请求下一轮同步，`POST /api/admin/v1/instruments` 和 `PUT /api/admin/v1/quotes` 保留为人工覆盖入口。都需要 operations/owner 身份及 `X-Admin-Reason`，每次写入审计。普通移动端无权改目录、行情或会员。

新增持仓只要求标的存在且展示权已确认；没有当前报价时仍可记录，但组合估值会返回不完整状态，不会用成本、0 或旧汇率填充。报价的实际来源、类型和时间在响应中保留。
