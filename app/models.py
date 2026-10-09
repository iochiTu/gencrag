"""Request/response models and the controlled vocabulary shared by the DB, parser and prompts."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

SKIN_TYPES = ["dry", "oily", "combination", "normal"]
CONCERNS = ["acne", "dryness", "redness", "pigmentation", "wrinkles", "pores", "dullness", "oiliness"]
AVOID = ["pregnancy", "fragrance", "retinoid", "aha", "bha", "alcohol", "essential_oil"]
CATEGORIES = ["cleanser", "toner", "essence", "serum", "cream", "sunscreen", "mask"]

# An avoid item can imply ingredient flags that must also be excluded
AVOID_EXPANSION = {"pregnancy": ["retinoid"]}

CONCERN_LABEL = {"acne": "breakouts", "dryness": "dryness", "redness": "redness", "pigmentation": "dark spots",
                 "wrinkles": "fine lines", "pores": "enlarged pores", "dullness": "dullness", "oiliness": "excess oil"}


class CustomerProfile(BaseModel):
    skin_type: Optional[Literal["dry", "oily", "combination", "normal"]] = None
    sensitive: bool = False
    concerns: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    budget_min: float = 0
    budget_max: Optional[float] = Field(None, description="Max price per item, in the database currency")
    categories: list[str] = Field(default_factory=list)
    note: str = ""

    @field_validator("concerns")
    @classmethod
    def _c(cls, v):
        return [x for x in dict.fromkeys(v) if x in CONCERNS]

    @field_validator("avoid")
    @classmethod
    def _a(cls, v):
        return [x for x in dict.fromkeys(v) if x in AVOID]

    @field_validator("categories")
    @classmethod
    def _cat(cls, v):
        return [x for x in dict.fromkeys(v) if x in CATEGORIES]

    def avoid_flags(self) -> list[str]:
        out = list(self.avoid)
        for a in self.avoid:
            out += AVOID_EXPANSION.get(a, [])
        return list(dict.fromkeys(out))


class RecommendRequest(BaseModel):
    profile: Optional[CustomerProfile] = None
    text: Optional[str] = Field(None, description="Free-text note from the employee")
    employee_id: Optional[str] = None
    top_n: int = Field(4, ge=1, le=6)
    debug: bool = False


class FeedbackRequest(BaseModel):
    log_id: int
    feedback: Literal["accepted", "swapped", "sold", "rejected"]
    note: Optional[str] = None
