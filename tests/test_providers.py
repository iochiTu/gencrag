import io
import json

import httpx
import pytest

from app.providers import BedrockProvider, OllamaProvider, extract_json


def test_ollama_request_shape_and_think_retry():
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append((req.url.path, body))
        if req.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.1] * 4 for _ in body["input"]]})
        if "think" in body:   # simulate an Ollama build that rejects the flag
            return httpx.Response(400, json={"error": "think value not supported"})
        return httpx.Response(200, json={"message": {"role": "assistant", "content": '{"picks": []}'}})

    p = OllamaProvider(client=httpx.Client(base_url="http://x", transport=httpx.MockTransport(handler)))
    assert len(p.embed(["a", "b"])) == 2
    assert p.chat("sys", "user") == '{"picks": []}'
    chat_calls = [b for path, b in calls if path == "/api/chat"]
    assert chat_calls[0]["think"] is False and "think" not in chat_calls[1]
    assert chat_calls[1]["format"] == "json" and chat_calls[1]["stream"] is False


class FakeBedrock:
    def __init__(self):
        self.bodies = []

    def invoke_model(self, modelId, body):
        b = json.loads(body)
        self.bodies.append((modelId, b))
        return {"body": io.BytesIO(json.dumps({"response_type": "embeddings_floats",
                                               "embeddings": [[0.0] * 3 for _ in b["texts"]]}).encode())}

    def converse(self, **kw):
        assert kw["messages"][0]["content"][0]["text"] == "hi"
        return {"output": {"message": {"content": [{"text": '{"ok": true}'}]}}}


def test_bedrock_cohere_v4_and_converse():
    fb = FakeBedrock()
    p = BedrockProvider(client=fb)
    assert len(p.embed(["x"] * 100, kind="query")) == 100       # two batches of <= 96
    assert len(fb.bodies) == 2 and fb.bodies[0][1]["input_type"] == "search_query"
    assert extract_json(p.chat("sys", "hi")) == {"ok": True}


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 2} hope this helps') == {"a": 2}
    assert extract_json("nope") is None


# ------------------------------------------------------------------ Microsoft Foundry
def _foundry(handler, monkeypatch, **env):
    from app import providers
    from app.config import settings
    vals = {"foundry_endpoint": "https://res.openai.azure.com/openai/v1/chat/completions?api-version=x",
            "foundry_api_key": "k123", "foundry_auth": "key", "foundry_chat_deployment": "gpt-4.1-mini",
            "foundry_parse_deployment": "gpt-4.1-mini", "foundry_embed_deployment": "emb-large", "embed_dim": 4, **env}
    import dataclasses
    monkeypatch.setattr(providers, "settings", dataclasses.replace(settings, **vals))   # test-only copy
    return providers.FoundryProvider(client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)


def test_foundry_endpoint_normalisation():
    from app.providers import foundry_base
    assert foundry_base("https://r.openai.azure.com/") == "https://r.openai.azure.com/openai/v1"
    assert foundry_base("https://r.openai.azure.com/openai/v1/") == "https://r.openai.azure.com/openai/v1"
    assert foundry_base("https://r.services.ai.azure.com/models") == "https://r.services.ai.azure.com/openai/v1"
    assert foundry_base("https://r.services.ai.azure.com/api/projects/p1") == "https://r.services.ai.azure.com/openai/v1"


def test_foundry_chat_key_auth_json_mode_and_plain_retry(monkeypatch):
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append((req.url.path, req.headers.get("api-key"), body))
        if "response_format" in body:      # e.g. a DeepSeek / reasoning deployment rejecting JSON mode
            return httpx.Response(400, json={"error": {"message": "response_format not supported"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"steps": []}'}}]})

    p = _foundry(handler, monkeypatch)
    assert p.chat("sys", "user") == '{"steps": []}'
    assert seen[0][0] == "/openai/v1/chat/completions" and seen[0][1] == "k123"
    assert seen[0][2]["model"] == "gpt-4.1-mini" and "response_format" not in seen[1][2]
    p.chat("sys", "again")
    assert "response_format" not in seen[2][2]                 # remembered: no wasted call next time


def test_foundry_errors_are_explained_and_429_retried(monkeypatch):
    from app.providers import ProviderError
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        if req.url.path.endswith("/embeddings"):
            return httpx.Response(404, json={"error": {"code": "DeploymentNotFound"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    p = _foundry(handler, monkeypatch)
    assert p.chat("s", "u") == "{}" and calls["n"] == 2
    with pytest.raises(ProviderError, match="deployment 'emb-large' was not found"):
        p.embed(["x"])
    p2 = _foundry(lambda r: httpx.Response(401), monkeypatch)
    with pytest.raises(ProviderError, match="rejected the credentials"):
        p2.chat("s", "u")
    p3 = _foundry(lambda r: httpx.Response(200), monkeypatch, foundry_api_key="")
    with pytest.raises(ProviderError, match="FOUNDRY_API_KEY is empty"):
        p3.chat("s", "u")


def test_foundry_embeddings_batches_dimensions_and_order(monkeypatch):
    bodies = []

    def handler(req):
        b = json.loads(req.content)
        bodies.append(b)
        if "dimensions" in b:
            return httpx.Response(400, json={"error": {"message": "dimensions not supported"}})
        data = [{"index": i, "embedding": [float(i)] * 4} for i in range(len(b["input"]))][::-1]   # out of order
        return httpx.Response(200, json={"data": data})

    p = _foundry(handler, monkeypatch)
    vecs = p.embed([f"t{i}" for i in range(20)])
    assert len(vecs) == 20 and vecs[0][0] == 0.0 and vecs[15][0] == 15.0 and vecs[16][0] == 0.0
    assert bodies[0]["dimensions"] == 4 and all("dimensions" not in b for b in bodies[1:])
