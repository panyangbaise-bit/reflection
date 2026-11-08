# Self-hosting guide

This directory contains an example single-host deployment: PostgreSQL, Redis
(broker/cache/auth-cache), the API, two Celery workers, dispatcher, scheduler,
an optional data collector, and an optional Caddy edge. It is an example — adapt
the settings to your own infrastructure and security requirements.

## 1. Prerequisites

- A Linux host (Ubuntu 22.04/24.04 tested). 2 vCPU / 2 GiB RAM is enough for the
  100-user scale; more is better for AI workloads.
- Docker Engine + Docker Compose v2.
- One or two DNS names (API and admin) with A records pointing at the host.
- Optional: an AI provider key (DeepSeek-compatible) or a gateway URL.

Do not build the image on a small (1–2 GiB) host: `uv sync` during `docker build`
can exhaust memory. Build on a workstation and transfer the image:

```sh
# Linux/amd64 workstations (or Apple Silicon with buildx emulation)
docker buildx build --platform linux/amd64 -t portfolio-agent-backend:beta --load .
docker save portfolio-agent-backend:beta | gzip -1 > /tmp/backend.tar.gz
rsync -avz --partial /tmp/backend.tar.gz user@your-host:/tmp/

# On the host
docker load < /tmp/backend.tar.gz
```

## 2. Generate secrets

`prepare.py` creates four private files under `secrets/beta/` (mode 0600) and
never overwrites an existing directory:

```sh
cd /path/to/repo
python3 deploy/beta/prepare.py --domain api.example.com --admin-domain admin.example.com
```

Set the values you need in `secrets/beta/runtime.env` (AI key, Apple bundle id,
phone-auth providers, collector enabled, etc.). Never commit `secrets/`.

## 3. Configure compose

```sh
cp deploy/beta/compose.env.example deploy/beta/compose.env   # created by prepare.py into secrets/
```

`secrets/beta/compose.env` at minimum contains:

```env
BETA_DOMAIN=api.example.com
ADMIN_DOMAIN=admin.example.com
# Optional on a 2 GiB host: keep workers single-concurrency
PA_WORKER_CONCURRENCY=1
PA_REPORT_WORKER_CONCURRENCY=1
```

The compose file publishes the API on `127.0.0.1:18000` only; put a reverse
proxy (Caddy snippet in `Caddyfile.host`, or the bundled `edge` service) in
front of it. The bundled `edge` service serves the Vue admin build from
`admin-web/dist` mounted read-only at `/srv/admin-web`.

## 4. Build the admin console (optional)

The Vue admin console is a static build committed under `admin-web/dist`; if you
change it, rebuild with Node 20+ and pnpm:

```sh
cd admin-web
pnpm install
pnpm build
```

## 5. Migrate and start

```sh
sh deploy/beta/compose.sh up -d --wait postgres cache broker
sh deploy/beta/compose.sh run --rm migrate
sh deploy/beta/compose.sh up -d --wait api worker worker-reports dispatcher scheduler
# Optional: market data collector (needs network access to public data sources)
sh deploy/beta/compose.sh --profile data up -d collector
sh deploy/beta/compose.sh ps
curl --fail https://api.example.com/health/ready
```

## 6. Operations

- Health: `curl https://api.example.com/health/ready` → `{"status":"ready"}`.
- Backups: `sh deploy/beta/backup.sh` dumps PostgreSQL into `artifacts/backups/`.
- Upgrade: transfer a new image, `docker load`, then
  `compose.sh up -d --force-recreate api worker worker-reports dispatcher scheduler`.
- Never run `compose.sh down -v` unless you intend to delete the database volume.
- Logs: `docker compose ... logs --tail=80 api worker worker-reports dispatcher scheduler`.
- The scheduler writes report out-box rows at fixed sessions: A/HK close
  (16:30 Asia/Shanghai), US close (16:30 America/New_York), and Saturday 09:00
  Asia/Shanghai weekly. Users are scheduled in ascending id order with a
  configurable per-batch delay (`PA_REPORT_DISPATCH_BATCH_USERS`,
  `PA_REPORT_DISPATCH_BATCH_INTERVAL_SECONDS`).
- Resource limits in `compose.yaml` are examples for a 2–4 GiB host. Tune them
  for your machine; see `docs/loadtest-reports.md` for measured numbers.
