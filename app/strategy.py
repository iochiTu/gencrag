"""Ranking strategy: how much customer fit vs. business value (margin, product priority) counts.

Managers set it in the admin portal with two sliders; it is stored in the `app_settings`
table and read on every request, so changes apply immediately without a restart.

  business_share  0-70 %   how much business value counts vs. customer fit
  priority_share  0-100 %  within business value: 0 = all profit margin, 100 = all product priority

  w_fit    = (1 - business) * 0.7      how well the product targets the customer's concerns
  w_sim    = (1 - business) * 0.3      semantic match to the note
  w_margin = business * (1 - priority)
  w_prio   = business * priority
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field

from .scoring import Weights

MAX_BUSINESS_SHARE = 70   # customer fit always keeps at least 30 % of the score

PRESETS: dict[str, dict] = {
    "customer_first": {"label": "Customer first", "business_share": 20, "priority_share": 50,
                       "help": "Best skin match wins; margin and priority only break ties."},
    "balanced":       {"label": "Balanced", "business_share": 35, "priority_share": 43,
                       "help": "Default. Fit leads, margin and priority matter when products are close."},
    "profit_focus":   {"label": "Profit focus", "business_share": 55, "priority_share": 25,
                       "help": "Higher-margin products move up whenever they still suit the customer."},
    "push_priority":  {"label": "Push priority products", "business_share": 50, "priority_share": 85,
                       "help": "New launches and priority SKUs move up; margin matters less."},
}


class RankingSettings(BaseModel):
    preset: Literal["customer_first", "balanced", "profit_focus", "push_priority", "custom"] = "balanced"
    business_share: int = Field(35, ge=0, le=MAX_BUSINESS_SHARE)
    priority_share: int = Field(43, ge=0, le=100)
    min_skin_fit: float = Field(0.0, ge=0, le=1, description="Hide products that match less than this share of the customer's concerns")
    use_llm_rerank: bool = True
    options_per_step: int = Field(3, ge=1, le=4, description="How many product choices staff see per routine step")
    min_days_before_expiry: int = Field(30, ge=0, le=365, description="Hide products expiring within this many days")
    unknown_ingredients: Literal["hide", "show"] = Field(
        "hide", description="Products without an ingredient list, when the customer must avoid something")

    def weights(self) -> Weights:
        b, p = self.business_share / 100, self.priority_share / 100
        return Weights(fit=round((1 - b) * 0.7, 4), sim=round((1 - b) * 0.3, 4),
                       margin=round(b * (1 - p), 4), prio=round(b * p, 4))

    def describe(self) -> dict:
        w = self.weights()
        return {"fit": w.fit, "similarity": w.sim, "margin": w.margin, "priority": w.prio}


def load_ranking(conn) -> RankingSettings:
    row = conn.execute("SELECT value FROM app_settings WHERE key = 'ranking'").fetchone()
    if not row:
        return RankingSettings()
    try:
        return RankingSettings(**row["value"])
    except Exception:
        return RankingSettings()


def save_ranking(conn, new: RankingSettings, actor: str) -> RankingSettings:
    old = load_ranking(conn)
    conn.execute(
        """INSERT INTO app_settings (key, value, updated_by, updated_at) VALUES ('ranking', %s, %s, now())
           ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_by = EXCLUDED.updated_by, updated_at = now()""",
        (json.dumps(new.model_dump()), actor),
    )
    audit(conn, actor, "ranking", "ranking", old.model_dump(), new.model_dump())
    return new


def audit(conn, actor: str, area: str, target: str, old, new) -> None:
    conn.execute(
        "INSERT INTO admin_audit (actor, area, target, old_value, new_value) VALUES (%s, %s, %s, %s, %s)",
        (actor, area, target, json.dumps(old, default=str), json.dumps(new, default=str)),
    )
