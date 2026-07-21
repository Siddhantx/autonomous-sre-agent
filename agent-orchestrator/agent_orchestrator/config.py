"""Environment-driven configuration for the APOE Active Agent Orchestrator.

All settings are sourced from environment variables (prefix ``APOE_``) via
``pydantic-settings``. Defaults intentionally mirror the lab ``docker-compose``
network so the orchestrator boots inside the enterprise lab with zero flags,
while every value — including credentials — remains overridable from the
environment. No secrets are hardcoded; the DSN defaults reference the lab's
well-known sandbox credentials only.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policies.yaml"


class Settings(BaseSettings):
    """Typed, validated orchestrator configuration."""

    model_config = SettingsConfigDict(
        env_prefix="APOE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Identity -----------------------------------------------------------
    service_name: str = "agent-orchestrator"
    environment: str = "lab"
    log_level: str = "INFO"

    # --- External systems ---------------------------------------------------
    prometheus_url: str = "http://prometheus:9090"
    postgres_dsn: str = Field(
        default="postgresql://apoe_user:apoe_secure_pass@postgres:5432/enterprise_db",
        description="asyncpg-compatible DSN. Override in production with a secret.",
    )
    redis_url: str = "redis://redis:6379/0"
    kafka_bootstrap: str = "kafka:29092"
    chaos_injector_url: str = "http://chaos-injector:9999"
    loki_url: str = "http://loki:3100"

    # --- OpenTelemetry ------------------------------------------------------
    otel_enabled: bool = True
    otel_endpoint: str = "http://otel-collector:4317"

    # --- Resilience / behaviour --------------------------------------------
    agent_timeout_seconds: float = 10.0
    max_retry_attempts: int = 5
    retry_backoff_seconds: float = 1.0
    retry_backoff_max_seconds: float = 15.0

    # --- Safety -------------------------------------------------------------
    safety_policy_path: Path = _DEFAULT_POLICY_PATH
    # When true, remediation is planned and evaluated but never executed.
    dry_run: bool = False

    # --- LLM investigator ---------------------------------------------------
    # provider: "anthropic" or "openai" (any OpenAI-compatible endpoint:
    # OpenAI, Ollama, vLLM). Investigator is disabled when llm_model is empty.
    llm_provider: str = "anthropic"
    llm_base_url: str = ""  # required for openai provider, e.g. http://localhost:11434/v1
    llm_model: str = ""
    llm_api_key: str = ""
    investigator_threshold: float = 0.7
    investigator_max_steps: int = 8
    investigator_max_tokens: int = 4000
    investigator_timeout_s: float = 30.0
    # Root of the lab service sources for the code_search tool.
    lab_source_path: Path = Path(__file__).resolve().parents[2] / "enterprise-lab"

    # --- Knowledge layer ----------------------------------------------------
    knowledge_db_path: Path = Path("apoe_knowledge.db")
    runbooks_path: Path = Path(__file__).resolve().parents[1] / "runbooks"

    # --- Governance ---------------------------------------------------------
    # Required by every mutating endpoint (X-API-Key header). Empty = all
    # mutating requests are rejected (default-deny).
    api_key: str = ""
    audit_log_path: Path = Path("apoe_audit.jsonl")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached ``Settings`` instance."""
    return Settings()
