"""FastAPI service.   uvicorn app.main:app --port 8000   then open http://localhost:8000"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from . import admin
from .config import settings
from .db import connect, save_feedback
from .models import FeedbackRequest, RecommendRequest
from .providers import ProviderError, embed_model_label, get_provider
from .recommender import recommend

app = FastAPI(title="K-Beauty Product Recommender", version="0.2.0")
provider = get_provider()
STATIC = Path(__file__).parent / "static"
admin.set_provider(provider)
app.include_router(admin.router)


@app.get("/")
def ui():
    return FileResponse(STATIC / "index.html")


@app.get("/admin")
def admin_ui():
    return FileResponse(STATIC / "admin.html")


@app.get("/health")
def health():
    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM products WHERE embedding IS NOT NULL").fetchone()["n"]
    return {"ok": True, "provider": settings.provider, "chat_model": provider.model_label("rank"),
            "embed_model": embed_model_label(), "embedded_products": n}


@app.post("/recommend")
def post_recommend(req: RecommendRequest):
    if not req.profile and not (req.text and req.text.strip()):
        raise HTTPException(400, "Send a profile, free text, or both.")
    try:
        with connect() as conn:
            return recommend(conn, provider, req)
    except ProviderError as e:
        raise HTTPException(503, str(e))


@app.post("/feedback")
def post_feedback(fb: FeedbackRequest):
    with connect() as conn:
        if not save_feedback(conn, fb.log_id, fb.feedback, fb.note):
            raise HTTPException(404, "log_id not found")
    return {"ok": True}
