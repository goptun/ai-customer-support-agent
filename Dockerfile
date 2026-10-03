FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN uv sync --frozen --no-dev

# Bake the embedding model into the image so the app never downloads it at runtime.
ENV FASTEMBED_CACHE_PATH=/app/models
RUN python -c "from support_agent.config import Settings; \
from support_agent.knowledge import FastEmbedEmbedder; \
FastEmbedEmbedder(Settings.model_fields['embedding_model'].default)"

RUN useradd --system --uid 1000 app && chown -R app /app/models
USER app

COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "support_agent.api:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
