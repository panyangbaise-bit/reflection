# 管理后台（Vue 3 静态前端）

`admin-web/` 是一个 Vue 3 + Vite + TypeScript + Naive UI 单页应用（hash 路由），通过现有 Admin API（`/api/admin/v1/*`）管理服务，不引入新的后端代码。浏览器只持有管理员令牌：所有请求带 `Authorization: Bearer <token>`，写操作另带 `X-Admin-Reason` 审计原因。

## 页面

| 页面 | 路由 | 主要接口 |
|---|---|---|
| 运行概览 | `#/` | `GET /overview` |
| 报告查看 | `#/reports` | `GET /users`、`GET /users/{id}/reports`、`GET /users/{id}/reports/{report_id}` |
| 触发报告 | `#/trigger` | `POST /users/{id}/reports`（`kind=daily|weekly`，`replay` 可强制重跑） |
| 任务状态 | `#/jobs` | `GET /jobs` |
| AI 调用轨迹 | `#/traces` | `GET /trace-users`、`GET /runs`、`GET /runs/{run_id}/tool-calls` |

## 本地开发

```sh
cd admin-web
pnpm install
pnpm dev     # 默认代理 /api → http://127.0.0.1:18000
```

后端在 `127.0.0.1:18000`（见 `deploy/beta/compose.yaml` 的 api 端口映射）或本地 `uv run portfolio-api` 的 8000 端口；如需改代理端口，编辑 `vite.config.ts`。

## 构建与部署

```sh
cd admin-web
pnpm build            # vue-tsc + vite build，产物在 dist/
```

把 `dist/` 交给任意静态文件服务器或 Caddy 托管，并将 `/api/admin/v1/*` 反代到 API：

- 容器部署：`deploy/beta/compose.yaml` 默认把 `admin-web/dist` 只读挂载到 Caddy 容器的 `/srv/admin-web`；
- 宿主机 Caddy：参考 `deploy/beta/Caddyfile.host` 的 admin 站点块（静态托管 + 反代 18000）。

## 鉴权

管理端与手机端凭据完全隔离：管理员令牌来自 `PA_ADMIN_CREDENTIALS`（哈希映射，按角色 owner/operations/support 授权）。令牌只存放在浏览器 `localStorage`；401/403 会提示凭据无效或权限不足。不要把管理员令牌下发给普通用户或写入代码。
