FROM ghcr.io/astral-sh/uv:0.12.17 AS uv
FROM python:3.12.14-slim-bookworm

COPY --from=uv /uv /uvx /bin/
WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/museum ./museum
RUN uv sync --frozen --no-dev \
    && mkdir -p /data \
    && useradd --uid 10001 --no-create-home museum \
    && chown museum:museum /data

USER museum
EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=5 \
  CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2)"]

CMD ["/app/.venv/bin/uvicorn", "museum.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
