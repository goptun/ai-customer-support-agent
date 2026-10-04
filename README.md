# ai-customer-support-agent

An AI agent that resolves Customer requests about their Orders in pt-BR (status, Cancellation, Refund, policy questions), escalating to an Attendant when it can't. Domain language: [GLOSSARY.md](GLOSSARY.md). Decisions: [docs/adr/](docs/adr/).

## Architecture

```
Customer / Attendant ──HTTP──▶ FastAPI (api.py) ──▶ Conversation graph (LangGraph, graph.py)
                                    │                    │  LLM: Haiku classifies, Sonnet writes replies (llm.py)
                                    │                    └─ tools (tools.py) ─▶ Postgres · logistics carrier · Knowledge Base
                                    └─ /approvals ─▶ approval workflow (LangGraph, approvals.py) ─▶ Refunds
```

- **API** (`api.py`): JWT-authenticated `/chat` for Customers, `/approvals` for Attendants, `/auth/dev-token` for local tokens.
- **Conversation graph** (`graph.py`): one run per Customer message on the Conversation's thread. The LLM only classifies the Intent and words the reply; every fact comes from a tool and every side effect is a tool call (idempotent, audited).
- **Approval workflow** (`approvals.py`, `decisions.py`): a Refund waiting for an Attendant pauses on a thread of its own ([ADR-0002](docs/adr/0002-separate-thread-per-approval-workflow.md)), so the Customer keeps chatting.
- **Postgres** (pgvector): domain data, the audit log, the Knowledge Base embeddings and the LangGraph checkpoints, so paused runs survive restarts ([ADR-0001](docs/adr/0001-postgres-checkpointer-for-paused-runs.md)). **Redis**: a disposable cache of each Conversation's working state.
- **Logistics carrier**: a mock HTTP service with deterministic scenarios (below).

### Conversation graph

Generated from the compiled graph (dashed edges are routing decisions):

<!-- graph:conversation -->
```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	classify_intent(classify_intent)
	resolve_context(resolve_context)
	act(act)
	consult_knowledge_base(consult_knowledge_base)
	check_refunds(check_refunds)
	await_confirmation(await_confirmation)
	cancel(cancel)
	escalate(escalate)
	respond(respond)
	__end__([<p>__end__</p>]):::last
	__start__ --> classify_intent;
	act -.-> escalate;
	act -.-> respond;
	await_confirmation -.-> cancel;
	await_confirmation -.-> classify_intent;
	await_confirmation -.-> respond;
	cancel --> respond;
	check_refunds --> respond;
	classify_intent -.-> check_refunds;
	classify_intent -.-> consult_knowledge_base;
	classify_intent -.-> escalate;
	classify_intent -.-> resolve_context;
	classify_intent -.-> respond;
	consult_knowledge_base --> respond;
	escalate --> respond;
	resolve_context -.-> act;
	resolve_context -.-> respond;
	respond -.-> __end__;
	respond -.-> await_confirmation;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```
<!-- /graph:conversation -->

| Node | Does |
|---|---|
| `classify_intent` | Classifies the message into Intents and entities; queues extra Intents, asks for Clarification when unsure. |
| `resolve_context` | Picks the Order: the one named, the Focused Order or the only recent one; otherwise gathers recent Orders for the reply to list. |
| `act` | Calls the tool for the Intent (order status, Cancellation preview, Refund request); repeated order-status failures escalate. |
| `consult_knowledge_base` | Retrieves policy sections for a policy question. |
| `check_refunds` | Reads the status of Refunds already requested. |
| `respond` | Writes the reply from the tool facts only. |
| `await_confirmation` | Pauses until the Customer confirms or denies a Cancellation. |
| `cancel` | Cancels the Order (and its automatic Refund). |
| `escalate` | Opens a Ticket for an Attendant. |

### Approval workflow

<!-- graph:approval -->
```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	await_decision(await_decision)
	settle(settle)
	__end__([<p>__end__</p>]):::last
	__start__ --> await_decision;
	await_decision --> settle;
	settle --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```
<!-- /graph:approval -->

After changing a graph, run `uv run python -m support_agent.diagram` to regenerate both diagrams; `tests/test_graph_diagram.py` fails while they are out of date.

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

Dev tokens are signed with `JWT_SECRET` and only exist for local use: `{"customer_id": ...}` for a Customer, `{"attendant_id": ...}` for an Attendant (seeded ids above).

Send the returned `conversation_id` to continue the same Conversation. Policy questions ("Qual o prazo para trocar um produto?") are answered from the Knowledge Base, naming the policy; when no policy covers the question the agent says so and offers a Ticket.

### Approving Refunds

An eligible Refund waits for an Attendant. With an Attendant token, list and decide them (a reason is required to reject):

```sh
ATT=$(curl -s -X POST localhost:8003/auth/dev-token \
  -H 'content-type: application/json' -d '{"attendant_id":"att_julia"}' | jq -r .access_token)
curl -s localhost:8003/approvals?status=pending -H "Authorization: Bearer $ATT"
curl -s -X POST localhost:8003/approvals/<approval_request_id> -H "Authorization: Bearer $ATT" \
  -H 'content-type: application/json' -d '{"decision":"approve"}'
```

Approving resumes the paused approval workflow and issues the Refund. The decision is appended to the Conversation; the Customer sees it in `notices` on their next `/chat` (even in a new Conversation) or by polling `GET /conversations/{id}/messages`.

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

### LLM provider

Anthropic by default (`ANTHROPIC_API_KEY`; Haiku 4.5 classifies, Sonnet 5.5 responds). Any OpenAI-compatible chat completions endpoint works instead (ADR-0004), e.g. the 9router on the VPS with its `arsenal-vps` combo:

```sh
LLM_PROVIDER=openai_compatible
LLM_BASE_URL=http://<router-host>:20128/v1   # with /v1
LLM_API_KEY=...                              # the router's key, if it checks one
CLASSIFIER_MODEL=arsenal-vps
RESPONDER_MODEL=arsenal-vps
```

The JSON the classifier must return is asked for in the prompt and parsed leniently; a reply that doesn't parse is treated as not understood (the agent asks again) and never as a confirmation.

### Tracing (LangSmith)

Off unless set. Add to `.env` and restart the app:

```sh
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=lsv2_...   # smith.langchain.com → Settings → API Keys
LANGSMITH_PROJECT=support-agent
```

Each Customer message is one trace named `conversation_turn` (filter by `conversation_id` in its metadata) with a span per graph node — latency, inputs and outputs — and the Claude calls and tool calls (args and result) as LLM and tool spans inside them. Outside Docker, export the variables (e.g. `uv run --env-file .env ...`): LangSmith reads the process environment, not `.env`.

## Evaluation

Scripted Conversations run against the real models, one command, run by hand (never in CI):

```sh
uv run python -m support_agent.evaluation                                 # every dataset
uv run python -m support_agent.evaluation eval/datasets/order_status.yaml # one dataset
```

It needs the LLM provider configured (environment or `.env`, see [LLM provider](#llm-provider)) and Docker: it starts throwaway Postgres and Redis containers (or uses `EVAL_DATABASE_URL` / `EVAL_REDIS_URL`, which must be disposable: the run seeds them and writes Conversations and Tickets), seeds them, indexes the Knowledge Base with the real embedding model and drives the app in process through `/chat`, with the logistics mock and the seed's clock (today = 01/09/2026). `--judge-model` picks the judge (default Sonnet 5.5 on Anthropic, the responder model on an OpenAI-compatible endpoint), on the same provider.

- **Datasets** (`eval/datasets/*.yaml`): each Conversation has a seeded Customer, fixed Customer turns and an expected outcome (`resolved | escalated_appropriate | clarification`); each turn may label the expected Intent, tool calls (name + the args that matter) and facts the reply must contain. Format notes at the top of `order_status.yaml`, which covers every logistics scenario above plus finding the Order, ownership and a human requested.
- **Metrics**: task completion (expected outcome and every required fact), tool-call accuracy per turn against the gold calls (retries collapsed, order ids in any format), Intent accuracy, p50/p95 latency end to end and per node, hallucination rate (an LLM judge lists reply claims the turn's tool outputs don't support) and escalation rate split into appropriate vs avoidable. Tool calls and node timings come from the turn's trace, recorded in process: no LangSmith account needed and nothing is sent.
- **Reports**: `eval/reports/<date>-<dataset>.json` and `.md`, with the model ids and commit, plus a ~10% sample of Conversations for a human to check the judge on. Commit them to compare runs over time.

## Develop

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

Tests use Postgres and Redis from `TEST_DATABASE_URL` / `TEST_REDIS_URL` when set (as in CI); otherwise they start throwaway containers via testcontainers (Docker required). Tests never call the real LLM or embedding model: they use a scripted fake LLM and a lexical fake embedder (`tests/fakes.py`) and the logistics mock in-process.
