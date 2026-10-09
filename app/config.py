"""Settings, read from environment variables or a .env file in the project root."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    database_url: str = _env("DATABASE_URL", "postgresql://rag:rag@localhost:5432/kbeauty")

    # ollama (local testing) | bedrock (production) | mock (no model at all, for CI)
    provider: str = _env("LLM_PROVIDER", "ollama").lower()

    # Every embedding model must output this size. 1024 works for bge-m3 (local),
    # Cohere Embed v4 and Titan Text Embeddings V2 (Bedrock).
    embed_dim: int = int(_env("EMBED_DIM", "1024"))

    # --- Ollama (local) ---
    ollama_url: str = _env("OLLAMA_URL", "http://localhost:11434")
    ollama_chat_model: str = _env("OLLAMA_CHAT_MODEL", "qwen3.5:9b")
    ollama_parse_model: str = _env("OLLAMA_PARSE_MODEL", "qwen3.5:9b")
    ollama_embed_model: str = _env("OLLAMA_EMBED_MODEL", "bge-m3")
    ollama_disable_thinking: bool = _env("OLLAMA_DISABLE_THINKING", "true").lower() == "true"
    ollama_timeout_s: float = float(_env("OLLAMA_TIMEOUT_S", "300"))

    # --- Amazon Bedrock (production) ---
    aws_region: str = _env("AWS_REGION", "ap-northeast-2")
    bedrock_chat_model: str = _env("BEDROCK_CHAT_MODEL", "anthropic.claude-sonnet-5")
    bedrock_parse_model: str = _env("BEDROCK_PARSE_MODEL", "anthropic.claude-haiku-4-5-20251001-v1:0")
    bedrock_embed_model: str = _env("BEDROCK_EMBED_MODEL", "cohere.embed-v4:0")

    # Prices in the database are in this currency (used for budget parsing and display)
    currency: str = _env("CURRENCY", "USD").upper()

    # --- Retrieval (ranking weights are set in the admin portal) ---
    retrieve_k: int = int(_env("RETRIEVE_K", "30"))
    shortlist_size: int = int(_env("SHORTLIST_SIZE", "10"))

    # --- Admin portal (/admin) ---
    admin_password: str = _env("ADMIN_PASSWORD", "")          # empty = portal disabled
    secret_key: str = _env("SECRET_KEY", "")                  # signs the admin login cookie
    admin_session_hours: int = int(_env("ADMIN_SESSION_HOURS", "12"))


settings = Settings()
