"""Run the evaluation suite against the real LLM; run manually, never in CI.

    uv run python -m support_agent.evaluation [eval/datasets/<name>.yaml ...]

Needs ANTHROPIC_API_KEY (from the environment or .env) and Docker: it starts throwaway
Postgres and Redis containers, unless EVAL_DATABASE_URL and EVAL_REDIS_URL point at
disposable ones (the run seeds them and writes Conversations and Tickets). The app runs in
process with the real models, the real embedding model and the logistics mock, on the seed's
clock. Writes a JSON and a Markdown report per dataset into eval/reports/.
"""

import argparse
import asyncio
import os
import secrets
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from anthropic import AsyncAnthropic
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import create_async_engine

from support_agent.api import create_app
from support_agent.config import Settings
from support_agent.db import session_factory
from support_agent.evaluation.dataset import Dataset, load_dataset
from support_agent.evaluation.judge import AnthropicJudge
from support_agent.evaluation.markdown import render_markdown
from support_agent.evaluation.report import ModelIds, Report, evaluate
from support_agent.knowledge import FastEmbedEmbedder, index_policies, load_policies
from support_agent.llm import AnthropicLLM
from support_agent.logistics_mock import create_app as create_logistics_mock
from support_agent.seed import ANCHOR, seed

DATASETS = Path("eval/datasets")
REPORTS = Path("eval/reports")
JUDGE_MODEL = "claude-sonnet-5-5"


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m support_agent.evaluation")
    parser.add_argument(
        "datasets", nargs="*", type=Path, help=f"YAML datasets (default: all in {DATASETS}/)"
    )
    parser.add_argument("--judge-model", default=JUDGE_MODEL)
    parser.add_argument("--out", type=Path, default=REPORTS)
    parser.add_argument("--seed", type=int, default=0, help="seed of the human review sample")
    args = parser.parse_args()
    datasets = [load_dataset(path) for path in args.datasets or sorted(DATASETS.glob("*.yaml"))]
    if not datasets:
        parser.error(f"no datasets in {DATASETS}/")

    settings = Settings(
        database_url="",  # set once the infrastructure is up
        redis_url="",
        jwt_secret=secrets.token_urlsafe(32),
        logistics_url="http://logistics.eval",
    )
    if settings.anthropic_api_key is None:
        parser.error("ANTHROPIC_API_KEY is required")

    with _infrastructure() as (database_url, redis_url):
        settings = settings.model_copy(
            update={"database_url": database_url, "redis_url": redis_url}
        )
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", settings.database_url)
        command.upgrade(config, "head")
        asyncio.run(_evaluate(settings, datasets, args.judge_model, args.out, args.seed))


@contextmanager
def _infrastructure() -> Iterator[tuple[str, str]]:
    database_url = os.environ.get("EVAL_DATABASE_URL")
    redis_url = os.environ.get("EVAL_REDIS_URL")
    if database_url and redis_url:
        yield database_url, redis_url
        return

    from testcontainers.community.postgres import PostgresContainer
    from testcontainers.community.redis import RedisContainer

    with (
        PostgresContainer("pgvector/pgvector:pg17", driver="psycopg") as pg,
        RedisContainer("redis:7-alpine") as rd,
    ):
        yield (
            pg.get_connection_url(),
            f"redis://{rd.get_container_host_ip()}:{rd.get_exposed_port(6379)}/0",
        )


async def _evaluate(
    settings: Settings, datasets: list[Dataset], judge_model: str, out: Path, sample_seed: int
) -> None:
    assert settings.anthropic_api_key is not None
    embedder = await asyncio.to_thread(FastEmbedEmbedder, settings.embedding_model)
    engine = create_async_engine(settings.database_url)
    try:
        sessions = session_factory(engine)
        await seed(sessions)
        await index_policies(sessions, embedder, load_policies())
    finally:
        await engine.dispose()

    anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())
    app = create_app(
        settings,
        llm=AnthropicLLM(anthropic, settings.classifier_model, settings.responder_model),
        embedder=embedder,
        logistics_transport=ASGITransport(app=create_logistics_mock()),
        today=ANCHOR.date,
    )
    models = ModelIds(
        classifier=settings.classifier_model, responder=settings.responder_model, judge=judge_model
    )
    async with (
        LifespanManager(app) as manager,
        AsyncClient(transport=ASGITransport(app=manager.app), base_url="http://eval") as client,
    ):
        for dataset in datasets:
            print(f"Running {dataset.name}: {len(dataset.conversations)} Conversations...")
            report = await evaluate(
                client,
                dataset,
                AnthropicJudge(anthropic, judge_model),
                models=models,
                started_at=datetime.now(UTC),
                git_commit=_git_commit(),
                seed=sample_seed,
            )
            for path in write_report(report, out):
                print(f"  wrote {path}")
            summary = report.summary
            print(
                f"  task completion {summary.task_completion_rate:.0%}, "
                f"tool-call accuracy {summary.tool_call_accuracy:.0%}, "
                f"turn p95 {summary.latency_ms.turn.p95:.0f} ms"
            )


def write_report(report: Report, directory: Path) -> tuple[Path, Path]:
    """Write `<started_at>-<dataset>.json` and `.md` into directory; returns both paths."""
    directory.mkdir(parents=True, exist_ok=True)
    stem = f"{report.started_at:%Y-%m-%dT%H%MZ}-{report.dataset}"
    json_path, markdown_path = directory / f"{stem}.json", directory / f"{stem}.md"
    json_path.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _git_commit() -> str | None:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return f"{commit}-dirty" if dirty else commit


if __name__ == "__main__":
    main()
