FROM python:3.12-slim
# 固定工具版本，避免每次构建通过 pip 重新引导安装 uv。
COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /usr/local/bin/
WORKDIR /app
COPY rag/pyproject.toml ./
COPY rag/uv.lock ./
ARG UV_HTTP_TIMEOUT=120
ARG UV_CONCURRENT_DOWNLOADS=8
ARG PYPI_DOWNLOAD_HOST=files.pythonhosted.org
ARG UV_DEFAULT_INDEX=https://pypi.org/simple
# 可选镜像只替换下载主机，保留锁定版本与 SHA256 校验值。
RUN python -c "import os; from pathlib import Path; p=Path('uv.lock'); p.write_text(p.read_text().replace('https://files.pythonhosted.org/', 'https://' + os.environ['PYPI_DOWNLOAD_HOST'] + '/'))"
# 依赖层与业务代码分开缓存，弱网络重试可复用已下载的包。
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --extra faithfulness --no-install-project --link-mode=copy
COPY rag/helpdesk ./helpdesk
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --extra faithfulness --link-mode=copy && useradd -u 10001 -m app
ENV PATH=/app/.venv/bin:$PATH
# 忠实度评分不使用 Git；允许 Ragas 导入其可选实验模块。
ENV GIT_PYTHON_REFRESH=quiet
ARG APP_REVISION=unknown
ENV APP_REVISION=$APP_REVISION
LABEL org.opencontainers.image.revision=$APP_REVISION
USER app
ENV RAG_HOST=0.0.0.0
CMD ["python","-m","helpdesk"]
