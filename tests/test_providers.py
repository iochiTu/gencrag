import io
import json

import httpx

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
