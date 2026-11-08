# 可选：阿里云手机号认证（后端集成）

本项目默认只要求「手机号 + 密码」登录。该文件描述可选的阿里云短信 / 号码认证接入，属于**部署方可选配置**，默认关闭；不配置时相关端点不工作，普通密码登录不受影响。

> 开源版 iOS 客户端**不包含**运营商号码认证 SDK（厂商 SDK 不允许随开源仓库分发）。若你需要一键登录/短信登录，请自行在客户端接入相应 SDK，并使用下面的服务端配置。服务端只提供签名校验与用户创建逻辑，不包含任何厂商 SDK。

## 服务端配置（`PA_` 前缀，见 `.env.example`）

| 变量 | 说明 |
|---|---|
| `PA_ALIYUN_ACCESS_KEY_ID` / `PA_ALIYUN_ACCESS_KEY_SECRET` | RAM 子账号密钥，仅授予所需权限（短信发送需 `dysms:SendSms`；号码认证需 `dypns:GetMobile`、`dypns:VerifyMobile`） |
| `PA_ALIYUN_SMS_ENABLED` / `PA_ALIYUN_SMS_SIGN_NAME` / `PA_ALIYUN_SMS_TEMPLATE_CODE` | 短信验证码登录，需要已审核的签名与模板 |
| `PA_ALIYUN_CARRIER_ENABLED` | 号码认证（运营商一键登录）开关 |
| `PA_PHONE_AUTH_PEPPER` | 至少 32 字符的随机 pepper，参与手机号哈希；泄露后需轮换并重建映射 |
| `PA_AUTH_CACHE_URL` | 生产环境启用手机号认证时必填的独立 Redis（AOF `everysec` 或 `always`、`noeviction`） |
| `PA_SMS_DAILY_LIMIT` / `PA_CARRIER_DAILY_LIMIT` | 全局每日发送/取号预算；另有每号 60s 冷却与每日上限 |
| `PA_TEST_PHONE_LOGIN_ENABLED` | 仅 Sandbox 的开发便利开关，配合固定测试号使用，生产禁用 |

相关端点（均在 `/api/mobile/v1` 下，启用前述开关后才可用）：

- `POST /auth/sms/send`、`POST /auth/sms/login`：短信验证码注册/登录；
- `POST /auth/carrier/login`、`POST /auth/carrier/change`：运营商取号登录与换号；
- `POST /auth/test-phone/login`：Sandbox 固定测试号登录（`PA_TEST_PHONE_LOGIN_ENABLED=true` 且 `PA_STORE_ENVIRONMENT=Sandbox`）。

## 安全注意事项

- 手机号在服务端以 pepper 哈希形式存储并参与唯一约束；原始手机号仅在用户资料中脱敏展示。
- 登录/换号/取号失败要有次数与频率上限；验证码与取号 token 一次性使用，防重放由 `auth-cache` 的原子操作保证。
- 不要把 AccessKey 写入客户端、仓库或镜像；用 secrets/env 注入。轮换密钥时只需更新运行环境变量。
- 上线前自行确认所用云服务与数据源的合规要求（当地法规、运营商政策、数据出境等）。
