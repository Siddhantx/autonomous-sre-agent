# APOE Active Agent Orchestrator — hardened runtime image.
# Pin by digest in production: docker pull python:3.11.9-slim && docker inspect
# --format='{{index .RepoDigests 0}}' python:3.11.9-slim  -> FROM python@sha256:...
FROM python:3.11.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only what the runtime needs — no tests, no evals, no venv.
COPY agent_orchestrator/ agent_orchestrator/
COPY runbooks/ runbooks/

# Non-root user; the app writes only its knowledge DB + audit log under /data.
RUN useradd --system --no-create-home apoe \
    && mkdir /data && chown apoe /data
USER apoe
ENV APOE_KNOWLEDGE_DB_PATH=/data/apoe_knowledge.db \
    APOE_AUDIT_LOG_PATH=/data/apoe_audit.jsonl

EXPOSE 8085

HEALTHCHECK --interval=15s --timeout=5s --retries=3 \
    CMD ["python", "-c", "import httpx; httpx.get('http://localhost:8085/health', timeout=3).raise_for_status()"]

# Exec form: uvicorn receives SIGTERM directly and shuts down gracefully.
CMD ["uvicorn", "agent_orchestrator.main:app", "--host", "0.0.0.0", "--port", "8085"]
