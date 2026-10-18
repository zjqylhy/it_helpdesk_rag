ARG NODE_BASE=node:22-bookworm-slim
ARG PYTHON_BASE=python:3.12-slim-bookworm
ARG NGINX_BASE=nginx:1.28-alpine

FROM ${NODE_BASE} AS frontend
WORKDIR /workspace/rewrite/frontend
COPY rewrite/frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY rewrite/frontend/ ./
RUN npm test && npm run build

FROM ${PYTHON_BASE} AS runtime
ARG PACKAGE_INDEX=https://pypi.org/simple
ARG PACKAGE_ARTIFACT_ROOT=https://files.pythonhosted.org/packages/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PATH=/workspace/rewrite/backend/.venv/bin:$PATH LANGSMITH_TRACING=false
WORKDIR /workspace/rewrite/backend
RUN pip install --no-cache-dir --index-url "$PACKAGE_INDEX" uv==0.12.7
COPY rewrite/backend/pyproject.toml rewrite/backend/uv.lock ./
# 仅重定向工件下载地址，保留版本与 SHA256；镜像中仍保留原锁文件。
RUN cp uv.lock /tmp/source-uv.lock \
    && sed -i "s#https://files.pythonhosted.org/packages/#$PACKAGE_ARTIFACT_ROOT#g" uv.lock \
    && UV_CONCURRENT_DOWNLOADS=4 UV_INDEX_URL="$PACKAGE_INDEX" uv sync --frozen --no-dev --no-install-project \
    && cp /tmp/source-uv.lock uv.lock
COPY rewrite/backend/src/ ./src/
RUN sed -i "s#https://files.pythonhosted.org/packages/#$PACKAGE_ARTIFACT_ROOT#g" uv.lock \
    && UV_INDEX_URL="$PACKAGE_INDEX" uv sync --frozen --no-dev \
    && cp /tmp/source-uv.lock uv.lock
COPY rewrite/db/ /workspace/rewrite/db/
RUN (getent passwd 10001 >/dev/null || useradd --uid 10001 --create-home helpdesk) \
    && mkdir -p /data/uploads && chown -R 10001:10001 /data \
    && chmod -R go-w /workspace/rewrite
ARG APP_REVISION=unknown
LABEL org.opencontainers.image.revision=$APP_REVISION
USER 10001
CMD ["python", "-m", "helpdesk.entrypoints.api"]

FROM runtime AS verify
USER root
RUN sed -i "s#https://files.pythonhosted.org/packages/#$PACKAGE_ARTIFACT_ROOT#g" uv.lock \
    && UV_CONCURRENT_DOWNLOADS=4 UV_INDEX_URL="$PACKAGE_INDEX" uv sync --frozen --group dev \
    && cp /tmp/source-uv.lock uv.lock
COPY rewrite/backend/tests/ ./tests/
COPY rewrite/contracts/ /workspace/rewrite/contracts/
COPY rewrite/scripts/ /workspace/rewrite/scripts/
COPY tools/check_runtime.py /workspace/tools/check_runtime.py
COPY --from=frontend /workspace/rewrite/frontend/ /workspace/rewrite/frontend/
COPY --from=frontend /usr/local/bin/node /usr/local/bin/node
CMD ["python", "-m", "pytest", "tests", "-q", "--tb=short"]

FROM verify AS browser
ARG DEBIAN_MIRROR=http://deb.debian.org
RUN sed -i "s#http://deb.debian.org#$DEBIAN_MIRROR#g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends chromium && rm -rf /var/lib/apt/lists/*
ENV HD_BROWSER=/usr/bin/chromium
CMD ["python", "/workspace/rewrite/scripts/verify_browser.py"]

FROM ${NGINX_BASE} AS web
RUN rm -rf /usr/share/nginx/html/*
COPY --from=frontend /workspace/rewrite/frontend/dist/ /usr/share/nginx/html/
COPY rewrite/deploy/nginx.conf /etc/nginx/conf.d/default.conf
ARG APP_REVISION=unknown
LABEL org.opencontainers.image.revision=$APP_REVISION
