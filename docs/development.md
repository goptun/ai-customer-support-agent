# Development

The reference for running, operating and evaluating the agent. Domain language: [GLOSSARY.md](../GLOSSARY.md). Decisions: [adr/](adr/).

## Run locally

```sh
cp .env.example .env   # set POSTGRES_PASSWORD, JWT_SECRET and the LLM provider (below)
docker compose up -d --build
```

Starts the app (127.0.0.1:8003), Postgres (pgvector), Redis and a mock logistics carrier (no published port). On start the app runs Alembic migrations and an idempotent seed of fictional Customers (`cus_ana`, `cus_bruno`, `cus_carla`, and `cus_diego` with a single recent Order), Attendants (`att_julia`, `att_marcos`) and Orders. It also indexes the Knowledge Base: the pt-BR policies in `src/support_agent/policies/` (one Markdown file per policy, one chunk per `##` section), embedded with a local multilingual model baked into the image (fastembed) and stored in pgvector. Only documents that changed are re-embedded.

```sh
TOKEN=$(curl -s -X POST localhost:8003/auth/dev-token \
  -H 'content-type: application/json' -d '{"customer_id":"cus_ana"}' | jq -r .access_token)
curl -s -X POST localhost:8003/chat -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"message":"Meu pedido ainda não chegou"}'
```

Dev tokens are signed with `JWT_SECRET` and only exist for local use (`DEV_TOKEN_ENABLED=true`; off by default, `/auth/dev-token` answers 404): `{"customer_id": ...}` for a Customer, `{"attendant_id": ...}` for an Attendant (seeded ids above).

Send the returned `conversation_id` to continue the same Conversation. Policy questions ("Qual o prazo para trocar um produto?") are answered from the Knowledge Base, naming the policy; when no policy covers the question the agent says so and offers a Ticket.

## Approving Refunds

An eligible Refund waits for an Attendant. With an Attendant token, list and decide them (a reason is required to reject):

```sh
ATT=$(curl -s -X POST localhost:8003/auth/dev-token \
  -H 'content-type: application/json' -d '{"attendant_id":"att_julia"}' | jq -r .access_token)
curl -s localhost:8003/approvals?status=pending -H "Authorization: Bearer $ATT"
curl -s -X POST localhost:8003/approvals/<approval_request_id> -H "Authorization: Bearer $ATT" \
  -H 'content-type: application/json' -d '{"decision":"approve"}'
```

Approving resumes the paused approval workflow and issues the Refund. The decision is appended to the Conversation; the Customer sees it in `notices` on their next `/chat` (even in a new Conversation) or by polling `GET /conversations/{id}/messages`.

## Logistics scenarios

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

## LLM provider

Anthropic by default (`ANTHROPIC_API_KEY`; Haiku 4.5 classifies, Sonnet 5.5 responds). Any OpenAI-compatible chat completions endpoint works instead ([ADR-0004](adr/0004-openai-compatible-llm-provider.md)), e.g. a self-hosted router that puts several models behind one endpoint:

```sh
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://<router-host>:<port>/v1   # with /v1
LLM_API_KEY=...                              # the router's key, if it checks one
CLASSIFIER_MODEL=<model>
RESPONDER_MODEL=<model>
LLM_TIMEOUT_SECONDS=180                      # per request; routed models can be slow
```

The JSON the classifier must return is asked for in the prompt and parsed leniently; a reply that doesn't parse is treated as not understood (the agent asks again) and never as a confirmation.

## Tracing (LangSmith)

Off unless set, and always off under Compose: `compose.yaml` is the production project and forces `LANGSMITH_TRACING=false` (and the legacy `LANGSMITH_TRACING_V2` / `LANGCHAIN_TRACING_V2`), since traces carry Customer messages. To trace the containers on a dev machine, put the setting in an untracked `compose.override.yaml` (gitignored; Compose loads it automatically, so never create one in production):

```yaml
services:
  app:
    environment:
      LANGSMITH_TRACING_V2: "true"   # langsmith reads *_TRACING_V2 before *_TRACING
```

and the key and project in `.env`, then restart the app:

```sh
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...   # smith.langchain.com → Settings → API Keys
LANGSMITH_PROJECT=support-agent
```

Each Customer message is one trace named `conversation_turn` (filter by `conversation_id` in its metadata) with a span per graph node — latency, inputs and outputs — and the LLM calls and tool calls (args and result) as LLM and tool spans inside them. Outside Docker, export the variables (e.g. `uv run --env-file .env ...`): LangSmith reads the process environment, not `.env`.

## Abuse protection

`POST /chat` answers `429` with a pt-BR `detail` and `Retry-After` when a Conversation sends more than `CONVERSATION_TURNS_PER_WINDOW` (10) turns in `CONVERSATION_RATE_WINDOW_SECONDS` (60), or when the LLM calls of the day (UTC, all Customers) reach `LLM_CALLS_PER_DAY` (2000 in the app, 300 under Compose, i.e. in production); the last turn of the day may go a few calls over rather than stop halfway. The body's `code` tells them apart: `conversation_rate_limited` or `llm_cap_exhausted`. Counters live in Redis.

An optional `Idempotency-Key` header makes a retry safe: the same key from the same Customer returns the original turn for `IDEMPOTENCY_TTL_SECONDS` (24 h) without running it again or counting it; the same key with a different body is `422`, and while the first request is still running (up to `IDEMPOTENCY_IN_FLIGHT_TTL_SECONDS`), `409`. A request that fails (including a `429`) frees its key for the retry.

## Evaluation

Scripted Conversations run against the real models, one command, run by hand (never in CI):

```sh
uv run python -m support_agent.evaluation                                 # every dataset
uv run python -m support_agent.evaluation eval/datasets/order_status.yaml # one dataset
```

It needs the LLM provider configured (environment or `.env`, see [LLM provider](#llm-provider)) and Docker: it starts throwaway Postgres and Redis containers (or uses `EVAL_DATABASE_URL` / `EVAL_REDIS_URL`, which must be disposable: the run seeds them and writes Conversations and Tickets), seeds them, indexes the Knowledge Base with the real embedding model and drives the app in process through `/chat`, with the logistics mock and the seed's clock (today = 01/09/2026). `--judge-model` picks the judge (default Sonnet 5.5 on Anthropic, the responder model on an OpenAI-compatible endpoint), on the same provider.

- **Datasets** (`eval/datasets/*.yaml`, ~67 Conversations): each Conversation has a seeded Customer, fixed Customer turns and an expected outcome (`resolved | escalated_appropriate | clarification`); each turn may label the expected Intent, tool calls (name + the args that matter) and facts the reply must contain, and may have an Attendant decide the Customer's pending Approval Requests first. Every Conversation starts from the seeded data. Format notes at the top of `order_status.yaml`. Coverage: `order_status` (every logistics scenario above, finding the Order, ownership), `cancellation` (confirm/deny/anything else, Orders that can't be cancelled), `refund` (every Refund Policy branch, amounts, approval and rejection, Refund status), `knowledge_base` (policy hits and misses), `conversation` (multiple Intents, Clarification, small talk, human requested, repeated tool failure).
- **Metrics**: task completion (expected outcome and every required fact), tool-call accuracy per turn against the gold calls (retries collapsed, order ids in any format), Intent accuracy, p50/p95 latency end to end and per node, hallucination rate (an LLM judge lists reply claims the turn's tool outputs don't support) and escalation rate split into appropriate vs avoidable. Tool calls and node timings come from the turn's trace, recorded in process: no LangSmith account needed and nothing is sent.
- **Reports**: `eval/reports/<date>-<dataset>.json` and `.md`, with the model ids and commit, plus a ~10% sample of Conversations for a human to check the judge on. A turn whose LLM reply fell back to the facts keeps the raw reply and the checks it failed (`empty`; `markup`: chat-template tokens such as `<|end|>` in the reply; `grounded`: the tokens beyond the facts; `complete`: the required facts not affirmed). Commit them to compare runs over time.

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests use Postgres and Redis from `TEST_DATABASE_URL` / `TEST_REDIS_URL` when set (as in CI); otherwise they start throwaway containers via testcontainers (Docker required). Tests never call the real LLM or embedding model: they use a scripted fake LLM and a lexical fake embedder (`tests/fakes.py`) and the logistics mock in-process.
