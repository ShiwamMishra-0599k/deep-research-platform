FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq5 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home lumen

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

USER lumen
EXPOSE 8080

# Apply migrations (idempotent, advisory-locked), then serve. A failed migration stops the container.
# On SIGTERM uvicorn stops accepting, waits for open research streams for SHUTDOWN_GRACE_SECONDS, then cancels what is left (the app saves an
# "interrupted" note). Keep it BELOW the orchestrator's stop timeout (ECS default 30 s, maximum 120 s) so the process exits by itself, not by SIGKILL.
ENV SHUTDOWN_GRACE_SECONDS=25
CMD ["sh", "-c", "python -m app.migrate && exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --no-proxy-headers --timeout-graceful-shutdown ${SHUTDOWN_GRACE_SECONDS}"]
