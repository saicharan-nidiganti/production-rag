# Production RAG as a container image (docs/operations.md#container-image).
#
#   docker run -p 8000:8000 -v rag-data:/data ghcr.io/saicharan-nidiganti/production-rag
#
# Dependencies come from uv.lock, exactly as locked. The service runs as an unprivileged
# user, keeps its SQLite database (and downloaded models) in /data, and applies migrations
# when it starts. With no settings it runs offline: hashing embeddings, extractive answers.
# The web interface is served at /.

# The web interface: static files, built once and copied into the image.
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY web ./
RUN npm run build


FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app
# Dependencies first, so this layer is reused until uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra local-embeddings --no-install-project
COPY README.md LICENSE ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --extra local-embeddings --no-editable


FROM python:3.12-slim

LABEL org.opencontainers.image.title="Production RAG" \
      org.opencontainers.image.description="Self-hosted RAG backend: hybrid retrieval with reranking, cited answers, abstention, streamed answers." \
      org.opencontainers.image.source="https://github.com/saicharan-nidiganti/production-rag" \
      org.opencontainers.image.licenses="MIT"

# The base image's tag lags the distribution's security fixes; take them at build time.
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --shell /usr/sbin/nologin rag \
    && mkdir /data && chown rag:rag /data
COPY --from=build /app/.venv /app/.venv
COPY --from=web /web/dist /app/web

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    RAG_DATABASE_URL=sqlite:////data/rag.db \
    RAG_WEB_DIR=/app/web \
    RAG_FASTEMBED_CACHE_DIR=/data/models

USER rag
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# One process; set RAG_API_KEY before exposing the port beyond your machine.
CMD ["rag", "serve", "--host", "0.0.0.0", "--port", "8000", "--migrate"]
