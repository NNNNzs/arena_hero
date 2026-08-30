FROM node:22-bookworm-slim AS frontend-build

WORKDIR /build/frontend

RUN corepack enable

COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY frontend/ ./
RUN pnpm build

FROM python:3.11-slim

LABEL org.opencontainers.image.title="Arena Hero Tactic Dashboard" \
      org.opencontainers.image.description="24/7 Arena Hero worker and dependency-free web dashboard"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARENA_HERO_HEALTH_HOST=0.0.0.0 \
    ARENA_HERO_HEALTH_PORT=8787

WORKDIR /app

# Runtime dependencies are built into the image. Application source is mounted
# by docker-compose.yml for development and is intentionally not copied here.
COPY pyproject.toml ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir \
       'arena-hero>=0.2.9,<0.3'

COPY docker-entrypoint.sh /usr/local/bin/arena-hero-entrypoint.sh

# Keep the generated Vue bundle outside the source bind mount used by Compose.
COPY --from=frontend-build /build/arena_tactic/web/static/app /app/frontend-build

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /app/runtime \
    && chown -R app:app /app \
    && chmod +x /usr/local/bin/arena-hero-entrypoint.sh

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import json,urllib.request; data=json.load(urllib.request.urlopen('http://127.0.0.1:8787/livez',timeout=3)); raise SystemExit(0 if data.get('running') else 1)"

VOLUME ["/app/runtime"]

ENTRYPOINT ["/usr/local/bin/arena-hero-entrypoint.sh"]
