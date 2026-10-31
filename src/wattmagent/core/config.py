"""Typed configuration. Everything comes from environment variables (WATTMAGENT_*)."""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WATTMAGENT_", env_file=".env", extra="ignore")

    # "rules": deterministic language handling, no network (the default).
    # "llm": use the model chain below, with the rules as the fallback.
    language_backend: str = "rules"

    # --- LLM via Hugging Face Inference Providers (OpenAI-compatible router)
    hf_token: SecretStr = SecretStr("")
    hf_base_url: str = "https://router.huggingface.co/v1"
    # Ordered fallback chain. Provider coverage changes: verify on the Hub.
    llm_models: str = "Qwen/Qwen2.5-7B-Instruct,meta-llama/Llama-3.1-8B-Instruct"
    max_output_tokens: int = 400
    request_timeout_s: float = 30.0
    daily_token_budget: int = 150_000  # our own cap, kept below free-tier limits
    cache_dir: Path = Path(".cache/llm")

    # --- API
    api_keys: str = ""  # comma-separated; the API refuses to start without one
    rate_limit_per_min: int = 20
    idempotency_ttl_s: int = 86_400
    price_csv: Path | None = None  # hourly price file the API plans from (timestamp,price_eur_mwh)
    timezone: str = "Europe/Berlin"
    audit_log: Path | None = None  # JSON Lines audit file; None keeps decisions in memory only

    @property
    def model_chain(self) -> list[str]:
        return [m.strip() for m in self.llm_models.split(",") if m.strip()]

    @property
    def api_key_set(self) -> frozenset[str]:
        return frozenset(k.strip() for k in self.api_keys.split(",") if k.strip())
