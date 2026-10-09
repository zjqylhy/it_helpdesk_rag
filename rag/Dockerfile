FROM python:3.12-slim
WORKDIR /app
COPY rag/pyproject.toml ./
COPY rag/helpdesk ./helpdesk
RUN pip install --no-cache-dir . && useradd -u 10001 -m app
USER app
CMD ["uvicorn","helpdesk.api:app","--host","0.0.0.0","--port","8000"]
