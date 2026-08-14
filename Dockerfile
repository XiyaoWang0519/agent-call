FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data /var/data \
    && chown app:app /data /var/data

COPY --from=ghcr.io/astral-sh/uv:0.9.27 /uv /usr/local/bin/uv
COPY --chown=app:app pyproject.toml uv.lock README.md ./
COPY --chown=app:app app ./app
RUN uv sync --frozen --no-dev && chown -R app:app /app

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000

USER app

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
