# ai-customer-support-agent

An AI agent that resolves Customer requests about their Orders in pt-BR (status, Cancellation, Refund, policy questions), escalating to an Attendant when it can't. Domain language: [GLOSSARY.md](GLOSSARY.md). Decisions: [docs/adr/](docs/adr/).

## Run locally

```sh
cp .env.example .env   # set POSTGRES_PASSWORD, JWT_SECRET and ANTHROPIC_API_KEY
docker compose up -d --build
```

Starts the app (127.0.0.1:8003), Postgres (pgvector), Redis and a mock logistics carrier (no published port). On start the app runs Alembic migrations and an idempotent seed of fictional Customers (`cus_ana`, `cus_bruno`, `cus_carla`, and `cus_diego` with a single recent Order) and Orders.

```sh
TOKEN=$(curl -s -X POST localhost:8003/auth/dev-token \
  -H 'content-type: application/json' -d '{"customer_id":"cus_ana"}' | jq -r .access_token)
curl -s -X POST localhost:8003/chat -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"message":"Meu pedido ainda não chegou"}'
```

Send the returned `conversation_id` to continue the same Conversation.

### Logistics scenarios

The mock carrier answers deterministically by tracking code; each seeded Dispatched Order exercises one scenario (`support_agent/logistics_mock.py`):

| Order | Customer | Scenario |
|---|---|---|
| ORD-1002 | cus_ana | in transit |
| ORD-2002 | cus_bruno | delayed |
| ORD-1001, ORD-3002 | cus_ana, cus_carla | delivered |
| ORD-1004 | cus_ana | lost |
| ORD-2004 | cus_bruno | returned |
| ORD-3003 | cus_carla | timeout |
| ORD-3004 | cus_carla | carrier 500 |
| ORD-3005 | cus_carla | carrier 404 |

ORD-1003, ORD-2001 and ORD-3001 are not Dispatched yet (no tracking).

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests use Postgres and Redis from `TEST_DATABASE_URL` / `TEST_REDIS_URL` when set (as in CI); otherwise they start throwaway containers via testcontainers (Docker required). Tests never call the real LLM: they use a scripted fake (`tests/fakes.py`) and the logistics mock in-process.
