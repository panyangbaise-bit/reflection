# admin-web · 复盘了后台管理前端

Vue 3 + Vite + TypeScript + vue-router（hash 路由）+ Naive UI + axios 的静态管理前端，
替代早期的 Streamlit 管理后台，
**完全复用现有 Admin API**（`/api/admin/v1/*`，Bearer 管理员令牌 + `X-Admin-Reason` 审计头），后端零改动。

## 环境要求

- Node.js ≥ 18（脚本由 PATH 中的 node 执行）
- pnpm（本仓库使用 DSH 工具链自带的 pnpm shim：`node pnpm <args>`；DSH 运行时路径变化时同步替换）

## 安装

```sh
cd admin-web
node pnpm install
# 网络超时时重试：
node pnpm install --registry https://registry.npmjs.org
```

`node_modules/`、`pnpm-lock.yaml`、`dist/` 均保留在目录内（部署静态文件直接来自 `dist/`）。

## 本地开发

```sh
node pnpm dev
# → http://127.0.0.1:5173
```

开发服务器把 `/api` 代理到 `http://127.0.0.1:18000`（compose 中 api 服务的宿主机端口）。
若在本地联调远端测试服，先建立 SSH 隧道：

```sh
ssh -L 18000:127.0.0.1:18000 root@203.0.113.10
```

页面打开后先粘贴管理员令牌（`secrets/beta/runtime.env` 中 `PA_ADMIN_CREDENTIALS` 对应的令牌），
保存在浏览器 localStorage（key `pa-admin-token`）。

## 构建（类型检查 + 产物）

```sh
node pnpm build   # vue-tsc --noEmit && vite build
```

产物输出到 `dist/`：`index.html` + `assets/*`（相对路径，hash 路由，可直接静态托管）。
零 TS 报错才算通过。

## 部署

### 方案 A：Compose edge 容器（推荐）

1. 本地构建：`node pnpm build`
2. 上传：`scp -r dist root@203.0.113.10:/opt/portfolio-beta/admin-web/`（覆盖旧 dist）
3. 服务器上确认 `secrets/beta/compose.env` 含 `ADMIN_DOMAIN=admin.example.com`
   （旧环境用 `echo 'ADMIN_DOMAIN=admin.example.com' >> secrets/beta/compose.env` 手动补；
   prepare.py 不会覆盖已有目录）。缺少 `ADMIN_DOMAIN` 时 edge 服务的 `${ADMIN_DOMAIN:?...}` 会让**所有** compose 命令直接失败。
4. 重启 Caddy：`cd /opt/portfolio-beta && ./deploy/beta/compose.sh up -d --no-deps edge`

`compose.yaml` 已把 `../../admin-web/dist` 只读挂载进 edge 容器 `/srv/admin-web`，
Caddyfile 用 `root * /srv/admin-web` + `try_files {path} /index.html` 托管，
并把 `/api/admin/v1/*` 反代到 `api:8000`。

### 方案 B：宿主机 Caddy（当前测试服用法）

1. 构建并上传 dist 到服务器 `/srv/admin-web/`
2. 用 `deploy/beta/Caddyfile.host` 的新 admin 站点块（静态托管 + 反代 127.0.0.1:18000）替换旧块后 `caddy reload`

## 页面与 API 对照

| 页面 | 路由（hash） | Admin API |
|---|---|---|
| 运行概览 | `#/` | `GET /overview` |
| 报告查看 | `#/reports` | `GET /users`、`GET /users/{id}/reports`、`GET /users/{id}/reports/{rid}` |
| 触发报告 | `#/trigger` | `GET /users`、`POST /users/{id}/reports`（`X-Admin-Reason`） |
| 任务状态 | `#/jobs` | `GET /jobs` |
| AI 调用轨迹 | `#/traces` | `GET /trace-users`、`GET /runs`、`GET /runs/{id}/tool-calls` |

鉴权：请求拦截器自动附加 `Authorization: Bearer <token>`（localStorage）。
401 → 提示输入令牌；403「管理员凭据无效」→ 清空令牌回到令牌输入视图；
其他 403（角色不足）→ 显示后端 message。POST 一律经 `postWithReason(path, body, reason)` 携带 `X-Admin-Reason`。

详细架构与回滚方案见 `../docs/admin-vue.md`。
