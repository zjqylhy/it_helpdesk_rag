FROM python:3.12-slim
WORKDIR /app
COPY rag/pyproject.toml ./
COPY rag/uv.lock ./
COPY rag/helpdesk ./helpdesk
RUN pip install --no-cache-dir uv==0.12.7 && uv sync --frozen --no-dev --extra faithfulness && useradd -u 10001 -m app
ENV PATH=/app/.venv/bin:$PATH
USER app
ENV RAG_HOST=0.0.0.0
CMD ["python","-m","helpdesk"]
