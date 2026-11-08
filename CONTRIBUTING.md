# Contributing

Thanks for your interest. This is a small, opinionated project; please open an
issue before starting a large change.

## Development

```sh
docker compose up -d --wait        # PostgreSQL + Redis for tests
uv sync --locked --extra market-data
export PA_MIGRATION_DATABASE_URL=postgresql+psycopg://postgres:local-owner-only@127.0.0.1:55432/portfolio
uv run alembic upgrade head
make test
make check
```

- Python 3.12, ruff (line length 100), pytest. Tests are integration tests and
  need the local PostgreSQL/Redis from `compose.yaml`.
- Never commit secrets, `.env`, `secrets/`, database dumps, or personal data.
- Keep financial rules explicit: missing/stale quotes or FX must never be
  filled with cost or zero; don't present close/delayed data as realtime.
- Admin UI changes: `cd admin-web && pnpm install && pnpm build`, commit `dist/`.
- Prompt/agent changes must keep the bounded ReAct guards (rounds, deadline,
  token budget, consent re-checks) intact.

## Pull requests

1. One focused change per PR with a short rationale.
2. Include/adjust tests; `make check` and `make test` must pass.
3. Describe any schema change, new environment variable, or operator action.

## Security

Please report vulnerabilities privately (see [SECURITY.md](SECURITY.md)), not in
a public issue.
