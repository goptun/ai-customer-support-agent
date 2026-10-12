# AI Customer Support Agent

[Português](README.md) · **[Try the demo →](https://matheusramos.dev/projects/support/)** (UI in pt-BR)

An AI agent that serves the Customers of a fictional Brazilian store in Portuguese: it checks Orders and tracking, cancels Orders not yet dispatched, requests Refunds that go through human approval and answers questions about the store's policies. When it can't resolve a request, it opens a Ticket for an Attendant.

![Replay of a demo Conversation: the Customer asks for a Refund on a lost Order, checks another Order and sees the Refund approved by the Attendant](docs/demo.gif)

## Highlights

- **The LLM only understands and words.** It classifies the message and writes the reply from tool outputs; every fact comes from a tool and every side effect is an idempotent, audited tool call. The Audit Log is append-only, enforced by a database trigger.
- **Human in the loop.** A Refund that needs approval pauses on a LangGraph thread of its own, checkpointed in Postgres, so the Customer keeps chatting while an Attendant decides ([ADR-0001](docs/adr/0001-postgres-checkpointer-for-paused-runs.md), [ADR-0002](docs/adr/0002-separate-thread-per-approval-workflow.md)).
- **Public demo, safely.** Each Demo Visitor passes Cloudflare Turnstile and gets a fictitious Customer plus a paired Attendant scoped to that Customer ([ADR-0005](docs/adr/0005-demo-visitor-is-customer-and-scoped-attendant.md)); layered rate limits and a daily LLM cap, after which the page plays a recorded Conversation; the database is rebuilt daily ([ADR-0006](docs/adr/0006-demo-reset-rebuilds-the-database.md)).
- **Measured.** ~67 scripted Conversations across 5 datasets run against the real models: in the latest run of each, 66 of 67 completed, tool-call and Intent accuracy 100%, 0% hallucination by an LLM judge, 0% avoidable escalations ([reports](eval/reports/)).
- **Provider-agnostic.** Anthropic by default, or any OpenAI-compatible endpoint ([ADR-0004](docs/adr/0004-openai-compatible-llm-provider.md)).

Architecture, decisions and the generated graph diagrams are in the [Portuguese README](README.md); domain language in [GLOSSARY.md](GLOSSARY.md).

## Run locally

```sh
cp .env.example .env   # set POSTGRES_PASSWORD, JWT_SECRET and the LLM provider
docker compose up -d --build
```

Then see [docs/development.md](docs/development.md) to chat through the API, approve Refunds, run the evaluation and the tests, and [docs/deploy.md](docs/deploy.md) for production.

## Stack

Python 3.12 · FastAPI · LangGraph · Anthropic API / OpenAI-compatible · PostgreSQL 17 + pgvector · fastembed · Redis · SQLAlchemy + Alembic · Docker Compose · nginx + Cloudflare Turnstile · pytest + testcontainers · uv, ruff, mypy · build-free HTML/CSS/JS front end

## License

[MIT](LICENSE)
