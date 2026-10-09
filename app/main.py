"""FastAPI service.   uvicorn app.main:app --host 0.0.0.0 --port 8000

  /         staff chat app (sign in with a staff account; works on phone, iPad and desktop)
  /admin    admin portal (ADMIN_PASSWORD)
  /docs     API documentation
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import admin, chat
from .analytics import track
from .auth import require_staff
from .auth import router as staff_router
from .config import settings
from .db import connect, save_feedback
from .models import FeedbackRequest, RecommendRequest
from .providers import ProviderError, embed_model_label, get_provider
from .recommender import recommend

app = FastAPI(title="K-Beauty Product Recommender", version="0.5.0")
provider = get_provider()
STATIC = Path(__file__).parent / "static"
admin.set_provider(provider)
chat.set_provider(provider)
app.include_router(admin.router)
app.include_router(staff_router)
app.include_router(chat.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/")
def chat_ui():
    return FileResponse(STATIC / "chat.html", headers=NO_CACHE)


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript", headers=NO_CACHE)


@app.get("/admin")
def admin_ui():
    return FileResponse(STATIC / "admin.html", headers=NO_CACHE)


@app.get("/health")
def health():
    with connect() as conn:
        n = conn.execute("SELECT count(*) AS n FROM products WHERE embedding IS NOT NULL").fetchone()["n"]
    return {"ok": True, "provider": settings.provider, "chat_model": provider.model_label("rank"),
            "embed_model": embed_model_label(), "embedded_products": n}


@app.post("/recommend")
def post_recommend(req: RecommendRequest, user: dict = Depends(require_staff)):
    """Structured recommendation API (the chat uses the same engine). Requires a staff login."""
    if not req.profile and not (req.text and req.text.strip()):
        raise HTTPException(400, "Send a profile, free text, or both.")
    req = req.model_copy(update={"employee_id": user["username"]})
    try:
        with connect() as conn:
            res = recommend(conn, provider, req)
            track(conn, "recommendation", user=user, intent="api", message=req.text, profile=res.get("profile"),
                  skus=[o["sku"] for st in res.get("steps", []) for o in st["options"]],
                  status=res["status"], used_llm=res.get("used_llm"), model=res.get("model"), log_id=res.get("log_id"),
                  latency_ms=res.get("elapsed_ms"))
            return res
    except ProviderError as e:
        raise HTTPException(503, str(e))


@app.post("/feedback")
def post_feedback(fb: FeedbackRequest, user: dict = Depends(require_staff)):
    with connect() as conn:
        if not save_feedback(conn, fb.log_id, fb.feedback, fb.note, fb.sku):
            raise HTTPException(404, "log_id not found")
        track(conn, "feedback", user=user, intent=fb.feedback, skus=[fb.sku] if fb.sku else None, log_id=fb.log_id)
    return {"ok": True}
