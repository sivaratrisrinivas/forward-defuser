# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv==0.11.32
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY evals ./evals
COPY scripts ./scripts
RUN uv sync --frozen --no-dev
RUN useradd --system --uid 10001 app && chown -R app /app
USER app
ENV PATH="/app/.venv/bin:$PATH" PORT=8080
EXPOSE 8080
# ROLE=api (default) serves webhooks; ROLE=worker runs the job worker; ROLE=all runs both in one process.
CMD ["sh", "-c", "defuser-migrate && case \"${ROLE:-api}\" in worker) exec defuser-worker ;; all) RUN_WORKER_IN_PROCESS=1 exec uvicorn defuser.app:app --host 0.0.0.0 --port $PORT ;; *) exec uvicorn defuser.app:app --host 0.0.0.0 --port $PORT ;; esac"]
