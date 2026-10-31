# Evidence API container. Runs as a non-root user with hash-pinned dependencies.
FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN useradd --uid 10001 --create-home app && mkdir /data && chown 10001:10001 /data
WORKDIR /app
COPY requirements/ requirements/
RUN pip install --require-hashes --no-deps -r requirements/api.txt
COPY wildfire/ wildfire/
COPY api/ api/
COPY config/ config/
USER 10001
ENV AUDIT_PATH=/data/audit.jsonl
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')"
CMD ["uvicorn", "api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
