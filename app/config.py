"""Settings, read from environment variables or a .env file in the project root."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    load_dotenv(ROOT / ".env", override=False, interpolate=False)


_load_dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = _env("DATABASE_URL", "postgresql://rag:rag@localhost:5432/kbeauty")

    # ollama (local testing) | bedrock (Amazon) | foundry (Microsoft Azure) | mock (no model, for CI)
    provider: str = _env("LLM_PROVIDER", "ollama").lower()

    # Every embedding model must output this size. 1024 works for bge-m3 (local),
    # Cohere Embed v4 and Titan Text Embeddings V2 (Bedrock).
    embed_dim: int = int(_env("EMBED_DIM", "1024"))

    # Ask reasoning models (DeepSeek Flash, GPT Luna / GPT-5+, Claude Sonnet 5.5, Qwen3) not to think:
    # thinking tokens are billed as output and slow answers down, and these tasks do not need them.
    disable_thinking: bool = _env("DISABLE_THINKING", "true").lower() == "true"

    # --- Ollama (local) ---
    ollama_url: str = _env("OLLAMA_URL", "http://localhost:11434")
    ollama_chat_model: str = _env("OLLAMA_CHAT_MODEL", "qwen3.5:9b")
    ollama_parse_model: str = _env("OLLAMA_PARSE_MODEL", "qwen3.5:9b")
    ollama_embed_model: str = _env("OLLAMA_EMBED_MODEL", "bge-m3")
    ollama_disable_thinking: bool = _env("OLLAMA_DISABLE_THINKING", _env("DISABLE_THINKING", "true")).lower() == "true"
    ollama_timeout_s: float = float(_env("OLLAMA_TIMEOUT_S", "300"))

    # --- Amazon Bedrock (production) ---
    aws_region: str = _env("AWS_REGION", "ap-northeast-2")
    bedrock_chat_model: str = _env("BEDROCK_CHAT_MODEL", "anthropic.claude-sonnet-5")
    bedrock_parse_model: str = _env("BEDROCK_PARSE_MODEL", "anthropic.claude-haiku-4-5-20251001-v1:0")
    bedrock_embed_model: str = _env("BEDROCK_EMBED_MODEL", "cohere.embed-v4:0")

    # --- Microsoft Foundry (Azure) ---
    # Endpoint from the Foundry portal, e.g. https://<resource>.openai.azure.com/openai/v1/
    foundry_endpoint: str = _env("FOUNDRY_ENDPOINT", "")
    foundry_auth: str = _env("FOUNDRY_AUTH", "key").lower()          # key | entra
    foundry_api_key: str = _env("FOUNDRY_API_KEY", "")
    foundry_chat_deployment: str = _env("FOUNDRY_CHAT_DEPLOYMENT", "gpt-4.1-mini")
    foundry_parse_deployment: str = _env("FOUNDRY_PARSE_DEPLOYMENT", "") or _env("FOUNDRY_CHAT_DEPLOYMENT", "gpt-4.1-mini")
    foundry_embed_deployment: str = _env("FOUNDRY_EMBED_DEPLOYMENT", "text-embedding-3-large")
    foundry_timeout_s: float = float(_env("FOUNDRY_TIMEOUT_S", "120"))

    # Prices in the database are in this currency (used for budget parsing and display)
    currency: str = _env("CURRENCY", "USD").upper()
    # How to read ambiguous dates like 03/04/2027 in imported files: MDY (Mar 4) or DMY (3 Apr).
    # Unambiguous dates (2027-03-04, 31/03/2027, Excel date cells) are always read correctly.
    date_order: str = _env("DATE_ORDER", "MDY").upper()

    # --- Retrieval (ranking weights are set in the admin portal) ---
    retrieve_k: int = int(_env("RETRIEVE_K", "30"))
    shortlist_size: int = int(_env("SHORTLIST_SIZE", "20"))   # candidates the AI chooses options from

    # --- Admin portal (/admin) ---
    admin_password: str = _env("ADMIN_PASSWORD", "")          # empty = portal disabled
    secret_key: str = _env("SECRET_KEY", "")                  # signs the admin login cookie
    admin_session_hours: int = int(_env("ADMIN_SESSION_HOURS", "12"))

    # --- Staff chat app (/) ---
    staff_session_days: int = int(_env("STAFF_SESSION_DAYS", "30"))
    brand_name: str = _env("BRAND_NAME", "Gen C Beauty")
    brand_logo_url: str = _env("BRAND_LOGO_URL", "https://cdn11.bigcommerce.com/s-kzkmuqjqk9/images/stencil/original/download1_1659212301__04676.original.png")


settings = Settings()
