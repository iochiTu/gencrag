"""Analytics event log. track() never raises: analytics must not break the chat for staff."""
from __future__ import annotations

import json
import logging

log = logging.getLogger("analytics")


def track(conn, event: str, *, user: dict | None = None, conversation_id: int | None = None, intent: str | None = None,
          message: str | None = None, profile: dict | None = None, skus: list[str] | None = None,
          result_count: int | None = None, status: str = "ok", latency_ms: int | None = None,
          used_llm: bool | None = None, model: str | None = None, log_id: int | None = None,
          meta: dict | None = None) -> None:
    try:
        conn.execute(
            """INSERT INTO analytics_events (event, user_id, username, conversation_id, intent, message, profile, skus,
                                             result_count, status, latency_ms, used_llm, model, log_id, meta)
               VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (event, (user or {}).get("id"), (user or {}).get("username"), conversation_id, intent,
             (message or "")[:2000] or None, json.dumps(profile) if profile is not None else None, skus,
             result_count, status, latency_ms, used_llm, model, log_id,
             json.dumps(meta, default=str) if meta is not None else None))
    except Exception as e:                      # e.g. table missing before `--init`; log and carry on
        log.warning("analytics event %s not recorded: %s", event, e)
