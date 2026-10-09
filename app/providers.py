"""LLM + embedding providers. Switch with LLM_PROVIDER = ollama | bedrock | mock.

- ollama : local testing (e.g. qwen3.5:9b for chat, bge-m3 for embeddings)
- bedrock: production on Amazon Bedrock (Claude / Qwen / DeepSeek via the Converse API,
           Cohere Embed v4 or Titan V2 for embeddings)
- mock   : no model at all; deterministic hash embeddings, chat returns nothing so the
           recommender uses its rule-based fallback. Useful for CI and DB testing.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Literal, Protocol

import httpx

from .config import settings

Role = Literal["parse", "rank"]
EmbedKind = Literal["document", "query"]


class Provider(Protocol):
    name: str

    def embed(self, texts: list[str], kind: EmbedKind = "document") -> list[list[float]]: ...
    def chat(self, system: str, user: str, role: Role = "rank") -> str: ...
    def model_label(self, role: Role) -> str: ...


# --------------------------------------------------------------------------- Ollama (local)
class ProviderError(RuntimeError):
    """A model call failed in a way the user can fix (service down, model not pulled, no access)."""


class OllamaProvider:
    name = "ollama"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(base_url=settings.ollama_url, timeout=settings.ollama_timeout_s)
        self._think_supported = True

    def model_label(self, role: Role) -> str:
        return settings.ollama_parse_model if role == "parse" else settings.ollama_chat_model

    def _post(self, path: str, body: dict) -> httpx.Response:
        try:
            return self.client.post(path, json=body)
        except httpx.ConnectError as e:
            raise ProviderError(f"Cannot reach Ollama at {settings.ollama_url}. Is Ollama running? "
                                f"(Windows: start the Ollama app, or run `ollama serve`)") from e
        except httpx.TimeoutException as e:
            raise ProviderError(f"Ollama timed out after {settings.ollama_timeout_s}s; the model may still be "
                                f"loading. Retry, or raise OLLAMA_TIMEOUT_S.") from e

    def _check(self, r: httpx.Response, model: str) -> None:
        if r.status_code == 404:
            raise ProviderError(f"Ollama model '{model}' is not installed. Run: ollama pull {model}")
        if r.status_code >= 400:
            raise ProviderError(f"Ollama error {r.status_code}: {r.text[:300]}")

    def embed(self, texts, kind="document"):
        out: list[list[float]] = []
        model = settings.ollama_embed_model
        for i in range(0, len(texts), 32):
            r = self._post("/api/embed", {"model": model, "input": texts[i:i + 32]})
            self._check(r, model)
            out.extend(r.json()["embeddings"])
        return out

    def chat(self, system, user, role="rank"):
        body = {
            "model": self.model_label(role),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }
        if settings.ollama_disable_thinking and self._think_supported:
            body["think"] = False  # Qwen3 / Qwen3.5 answer much faster without the thinking pass
        r = self._post("/api/chat", body)
        if r.status_code == 400 and "think" in body:
            # model (or older Ollama) does not support the think flag -> retry without it
            self._think_supported = False
            body.pop("think")
            r = self._post("/api/chat", body)
        self._check(r, body["model"])
        return r.json()["message"]["content"]


# --------------------------------------------------------------------------- Amazon Bedrock
class BedrockProvider:
    name = "bedrock"

    def __init__(self, client=None):
        if client is None:
            import boto3
            client = boto3.client("bedrock-runtime", region_name=settings.aws_region)
        self.rt = client

    def model_label(self, role: Role) -> str:
        return settings.bedrock_parse_model if role == "parse" else settings.bedrock_chat_model

    def embed(self, texts, kind="document"):
        model = settings.bedrock_embed_model
        if "titan-embed" in model:  # Titan V2: one text per call
            out = []
            for t in texts:
                body = {"inputText": t, "dimensions": settings.embed_dim, "normalize": True}
                resp = self.rt.invoke_model(modelId=model, body=json.dumps(body))
                out.append(json.loads(resp["body"].read())["embedding"])
            return out
        out = []  # Cohere Embed v3 / v4: up to 96 texts per call
        for i in range(0, len(texts), 96):
            body = {
                "texts": texts[i:i + 96],
                "input_type": "search_document" if kind == "document" else "search_query",
                "embedding_types": ["float"],
            }
            if "embed-v4" in model:
                body["output_dimension"] = settings.embed_dim
                body["truncate"] = "RIGHT"
            resp = self.rt.invoke_model(modelId=model, body=json.dumps(body))
            emb = json.loads(resp["body"].read())["embeddings"]
            out.extend(emb["float"] if isinstance(emb, dict) else emb)
        return out

    def chat(self, system, user, role="rank"):
        resp = self.rt.converse(
            modelId=self.model_label(role),
            system=[{"text": system}],
            messages=[{"role": "user", "content": [{"text": user}]}],
            inferenceConfig={"maxTokens": 1500, "temperature": 0.2},
        )
        parts = resp["output"]["message"]["content"]
        return "".join(p.get("text", "") for p in parts)


# --------------------------------------------------------------------------- Mock (no model)
class MockProvider:
    name = "mock"

    def model_label(self, role: Role) -> str:
        return "mock"

    def embed(self, texts, kind="document"):
        return [_hash_embed(t, settings.embed_dim) for t in texts]

    def chat(self, system, user, role="rank"):
        return ""  # forces the deterministic fallback


def _hash_embed(text: str, dim: int) -> list[float]:
    """Bag of words hashed into `dim` buckets. Not semantic, but stable."""
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    v = [0.0] * dim
    for tok in tokens:
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % dim] += 1.0 if (h >> 64) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def get_provider() -> Provider:
    if settings.provider == "bedrock":
        return BedrockProvider()
    if settings.provider == "mock":
        return MockProvider()
    return OllamaProvider()


def embed_model_label() -> str:
    return {"bedrock": settings.bedrock_embed_model, "mock": "mock-hash"}.get(
        settings.provider, settings.ollama_embed_model)


def extract_json(text: str) -> dict | None:
    """Parse a JSON object out of an LLM reply (handles <think> blocks and ```json fences)."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None
