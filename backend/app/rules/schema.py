"""Policy schema: the business limits, validated on load so a typo can't silently weaken a rule."""

from datetime import date, time
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_POLICY_FILE = Path(__file__).with_name("default_policy.yaml")

OFFER_TYPES = {
    "CART_NUDGE",
    "BONUS_POINTS",
    "FREE_SHIPPING",
    "PCT_OFF",
    "FLAT_OFF",
    "UPI_CASHBACK",
    "WIN_BACK",
    "SECOND_ORDER",
    "POINTS_EXPIRY",
    "EARLY_ACCESS",
}
PERCENT_TYPES = {"PCT_OFF", "WIN_BACK"}
RUPEE_TYPES = {"FLAT_OFF", "SECOND_ORDER", "UPI_CASHBACK"}
LIFECYCLES = {"new", "vip", "active", "at_risk", "lapsed", "window_shopper", "bargain_hunter"}
UPLIFT_GROUPS = {"persuadable", "sure_thing", "lost_cause", "sleeping_dog"}


class Strict(BaseModel):
    model_config = {"extra": "forbid"}


class QuietHours(Strict):
    start: time
    end: time

    def contains(self, t: time) -> bool:
        if self.start <= self.end:
            return self.start <= t < self.end
        return t >= self.start or t < self.end  # wraps midnight, e.g. 21:00–09:00


class Contact(Strict):
    quiet_hours: QuietHours
    max_offers_per_7_days: int = Field(ge=0)
    min_days_between_offers: int = Field(ge=0)
    cooldown_after_purchase_days: int = Field(ge=0)
    holdout_share: float = Field(ge=0, le=0.5)
    message_cost_inr: float = Field(ge=0)


class Budget(Strict):
    daily_inr: float = Field(ge=0)
    max_cost_per_offer_inr: float = Field(ge=0)
    max_cost_share_of_clv: float = Field(ge=0, le=1)
    clv_cap_floor_inr: float = Field(ge=0)


class Targeting(Strict):
    never_contact_uplift_groups: list[str]
    sure_thing_allowed_types: list[str]
    min_expected_profit_inr: float
    retention_value_weight: float = Field(ge=0, le=2)
    new_account_days: int = Field(ge=0)
    new_account_max_cost_inr: float = Field(ge=0)

    @field_validator("never_contact_uplift_groups")
    @classmethod
    def known_groups(cls, v: list[str]) -> list[str]:
        unknown = set(v) - UPLIFT_GROUPS
        if unknown:
            raise ValueError(f"unknown uplift groups: {sorted(unknown)}")
        return v

    @field_validator("sure_thing_allowed_types")
    @classmethod
    def known_types(cls, v: list[str]) -> list[str]:
        unknown = set(v) - OFFER_TYPES
        if unknown:
            raise ValueError(f"unknown offer types: {sorted(unknown)}")
        return v


class CategoryRule(Strict):
    max_discount_pct: float = Field(ge=0, le=90)


class ValueRange(Strict):
    min: float
    max: float
    default: float

    @model_validator(mode="after")
    def ordered(self) -> "ValueRange":
        if not self.min <= self.default <= self.max:
            raise ValueError(f"need min ≤ default ≤ max, got {self.min}, {self.default}, {self.max}")
        return self


class OfferTemplate(Strict):
    id: str
    type: str
    label: str
    value: ValueRange
    validity_days: int = Field(ge=1, le=30)
    enabled: bool = True
    min_order_inr: float = 0
    max_discount_inr: float | None = None
    only_lifecycles: list[str] | None = None
    only_cod_customers: bool = False
    requires_expiring_points: bool = False

    @field_validator("type")
    @classmethod
    def known_type(cls, v: str) -> str:
        if v not in OFFER_TYPES:
            raise ValueError(f"unknown offer type {v!r}")
        return v

    @field_validator("only_lifecycles")
    @classmethod
    def known_lifecycles(cls, v: list[str] | None) -> list[str] | None:
        if v and set(v) - LIFECYCLES:
            raise ValueError(f"unknown lifecycles: {sorted(set(v) - LIFECYCLES)}")
        return v


class Messages(Strict):
    max_length: int = Field(ge=40)
    max_emojis: int = Field(ge=0)
    languages: list[str]
    allowed_link_domains: list[str]
    banned_phrases: list[str]
    false_urgency_patterns: list[str]
    competitors: list[str]


class FestiveOverride(Strict):
    name: str
    start: date
    end: date
    budget_multiplier: float = Field(ge=0, default=1.0)
    category_caps: dict[str, float] = {}


class Policy(Strict):
    version: int
    name: str
    updated_by: str
    updated_at: date
    contact: Contact
    budget: Budget
    targeting: Targeting
    categories: dict[str, CategoryRule]
    offers: list[OfferTemplate]
    fallbacks: dict[str, str]
    messages: Messages
    festive: list[FestiveOverride] = []

    @model_validator(mode="after")
    def consistent(self) -> "Policy":
        ids = [o.id for o in self.offers]
        if len(ids) != len(set(ids)):
            raise ValueError("offer ids must be unique")
        for lifecycle, offer_id in self.fallbacks.items():
            if lifecycle not in LIFECYCLES:
                raise ValueError(f"fallback for unknown lifecycle {lifecycle!r}")
            if offer_id != "none" and offer_id not in ids:
                raise ValueError(f"fallback {lifecycle!r} points to unknown offer {offer_id!r}")
        for f in self.festive:
            unknown = set(f.category_caps) - set(self.categories)
            if unknown:
                raise ValueError(f"festive override {f.name!r} has unknown categories {sorted(unknown)}")
        return self

    def offer(self, offer_id: str) -> OfferTemplate | None:
        return next((o for o in self.offers if o.id == offer_id), None)

    def festive_on(self, day: date) -> FestiveOverride | None:
        return next((f for f in self.festive if f.start <= day <= f.end), None)

    def category_cap(self, category: str, day: date) -> float:
        festive = self.festive_on(day)
        if festive and category in festive.category_caps:
            return festive.category_caps[category]
        rule = self.categories.get(category)
        return rule.max_discount_pct if rule else 0.0

    def daily_budget(self, day: date) -> float:
        festive = self.festive_on(day)
        return self.budget.daily_inr * (festive.budget_multiplier if festive else 1.0)


def load_policy(path: Path | str = DEFAULT_POLICY_FILE) -> Policy:
    return Policy.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))
