# ai-customer-support-agent

An AI agent that resolves Customer requests about their Orders in pt-BR (status, Cancellation, Refund, policy questions), escalating to an Attendant when it can't. Domain language: [GLOSSARY.md](GLOSSARY.md). Decisions: [docs/adr/](docs/adr/).

## Run locally

```sh
cp .env.example .env   # set POSTGRES_PASSWORD and JWT_SECRET
docker compose up -d --build
```

Starts the app (127.0.0.1:8003), Postgres (pgvector) and Redis. On start the app runs Alembic migrations and an idempotent seed of fictional Customers (`cus_ana`, `cus_bruno`, `cus_carla`) and Orders.

```sh
TOKEN=$(curl -s -X POST localhost:8003/auth/dev-token \
  -H 'content-type: application/json' -d '{"customer_id":"cus_ana"}' | jq -r .access_token)
curl -s -X POST localhost:8003/chat -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"message":"Meu pedido ainda não chegou"}'
```

Send the returned `conversation_id` to continue the same Conversation.

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests use Postgres and Redis from `TEST_DATABASE_URL` / `TEST_REDIS_URL` when set (as in CI); otherwise they start throwaway containers via testcontainers (Docker required).
