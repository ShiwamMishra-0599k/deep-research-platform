FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --no-create-home lumen

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

USER lumen
EXPOSE 8004

# Liveness only: /health is the single unauthenticated endpoint.
HEALTHCHECK --interval=15s --timeout=4s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8004/health', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "research_agent.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8004"]
