"""How much an offer is expected to change a customer's behaviour, and what it is worth (Option A).

* **Who**: the pooled "any offer" uplift model (passed its held-out test, ×1.69) gives each customer's
  general responsiveness, `uplift_any`.
* **Which**: each offer type's strength comes from the *measured* average effect in the past randomised
  campaigns. A customer's uplift for offer k = uplift_any × (measured effect of k ÷ average measured effect).
* A per-person model replaces this only for offer types whose model beat random targeting on held-out
  customers (`TRUST_MULTIPLIER`); that list is re-derived from every training report.
* Offer types never tested in a campaign borrow the closest tested type with a discount (`PROXIES`), and
  are labelled "assumed" in every explanation.

Profit of offer k for a customer (₹, over the offer window):
    uplift_k × (order margin + retention value)  −  P(buy with offer) × cost_k  −  message cost
"""

import json
from dataclasses import dataclass
from pathlib import Path

from app.data.paths import CALIBRATION_DIR

MODEL_REPORT = CALIBRATION_DIR / "model_report.json"
TRUST_MULTIPLIER = 1.1  # per-person model must put ≥ 1.1× the average uplift into its top 30%

ARM_TYPE = {
    "nudge": "CART_NUDGE",
    "points2x": "BONUS_POINTS",
    "freeship": "FREE_SHIPPING",
    "pct10": "PCT_OFF",
    "flat100": "FLAT_OFF",
}
TESTED_VALUE = {"CART_NUDGE": 0, "BONUS_POINTS": 2, "FREE_SHIPPING": 40, "PCT_OFF": 10, "FLAT_OFF": 100}
# untested type -> (tested type it resembles, discount on its effect)
PROXIES = {
    "UPI_CASHBACK": ("FLAT_OFF", 0.8),
    "WIN_BACK": ("PCT_OFF", 1.0),
    "SECOND_ORDER": ("FLAT_OFF", 1.0),
    "POINTS_EXPIRY": ("CART_NUDGE", 1.0),
    "EARLY_ACCESS": ("CART_NUDGE", 0.8),
}
VALUE_ELASTICITY = 0.5  # assumed diminishing returns when an offer is deeper/shallower than the tested one
POINT_VALUE_INR = 1.0


@dataclass(frozen=True)
class MeasuredEffect:
    offer_type: str
    effect: float  # treated − control, 14-day purchase rate
    ci95: tuple[float, float]
    tested_value: float
    per_person_trusted: bool
    targeting_multiplier: float | None


@dataclass(frozen=True)
class OfferEffects:
    measured: dict[str, MeasuredEffect]
    average_effect: float
    source: str

    @classmethod
    def from_report(cls, path: Path = MODEL_REPORT) -> "OfferEffects":
        report = json.loads(Path(path).read_text(encoding="utf-8"))
        arms, holdout = report["uplift"]["arms"], report["uplift"]["holdout"]
        measured = {}
        for arm, offer_type in ARM_TYPE.items():
            mult = holdout.get(offer_type, {}).get("targeting_multiplier")
            measured[offer_type] = MeasuredEffect(
                offer_type=offer_type,
                effect=arms[arm]["effect"],
                ci95=tuple(arms[arm]["ci95"]),
                tested_value=TESTED_VALUE[offer_type],
                per_person_trusted=mult is not None and mult >= TRUST_MULTIPLIER,
                targeting_multiplier=mult,
            )
        average = sum(m.effect for m in measured.values()) / len(measured)
        return cls(measured, average, f"model_report.json (trained {report.get('trained_at', '?')})")

    def basis(self, offer_type: str) -> tuple[MeasuredEffect, float, bool]:
        """(tested effect it is based on, proxy discount, is_assumed)."""
        if offer_type in self.measured:
            return self.measured[offer_type], 1.0, False
        base, discount = PROXIES[offer_type]
        return self.measured[base], discount, True


@dataclass(frozen=True)
class Economics:
    uplift: float  # extra probability of buying in the offer window, because of the offer
    p_buy_with_offer: float
    cost_if_used_inr: float
    expected_cost_inr: float
    value_if_buy_inr: float
    expected_profit_inr: float
    basis: str  # plain-English source of the uplift estimate


def order_value(ctx, template) -> float:
    """Basket the offer would apply to: the customer's usual order, at least the offer's minimum order."""
    return max(ctx.aov_inr, template.min_order_inr, 1.0)


def cost_if_used(ctx, template, value: float) -> float:
    order = order_value(ctx, template)
    kind = template.type
    if kind in ("PCT_OFF", "WIN_BACK"):
        cost = order * value / 100
        return min(cost, template.max_discount_inr) if template.max_discount_inr else cost
    if kind in ("FLAT_OFF", "SECOND_ORDER"):
        return float(value)
    if kind == "UPI_CASHBACK":
        return min(float(value), order * 0.10)
    if kind == "FREE_SHIPPING":
        return float(value)
    if kind == "BONUS_POINTS":
        return order / 100 * max(value - 1, 0) * POINT_VALUE_INR
    return 0.0


def discount_pct_of_order(ctx, template, value: float) -> float:
    """Monetary discount as % of the basket — what the category cap is checked against."""
    if template.type not in ("PCT_OFF", "WIN_BACK", "FLAT_OFF", "SECOND_ORDER", "UPI_CASHBACK"):
        return 0.0
    return 100 * cost_if_used(ctx, template, value) / order_value(ctx, template)


def economics(ctx, template, value: float, effects: OfferEffects, policy) -> Economics:
    base, proxy_discount, assumed = effects.basis(template.type)
    if base.per_person_trusted and not assumed and template.type in ctx.uplift_by_type:
        uplift = ctx.uplift_by_type[template.type]
        basis = f"per-person model (beat random ×{base.targeting_multiplier} on held-out customers)"
    else:
        share = base.effect / effects.average_effect if effects.average_effect > 0 else 1.0
        uplift = ctx.uplift_any * share * proxy_discount
        lo, hi = base.ci95
        basis = (
            f"customer responsiveness × measured {base.offer_type} effect "
            f"({base.effect * 100:+.1f} pp, 95% CI {lo * 100:+.1f} to {hi * 100:+.1f})"
        )
        if assumed:
            basis = f"assumed like {base.offer_type} (×{proxy_discount}), untested: " + basis
    if base.tested_value and value != base.tested_value and value > 0:
        uplift *= min((value / base.tested_value) ** VALUE_ELASTICITY, 2.0)
    uplift = float(min(max(uplift, -ctx.p_buy), 1 - ctx.p_buy))

    p_with = ctx.p_buy + uplift
    cost = cost_if_used(ctx, template, value)
    order_margin = order_value(ctx, template) * ctx.weighted_margin_pct / 100
    retention = policy.targeting.retention_value_weight * ctx.churn_60d * ctx.quarterly_margin_inr
    value_if_buy = order_margin + retention
    expected_cost = p_with * cost
    profit = uplift * value_if_buy - expected_cost - policy.contact.message_cost_inr
    return Economics(
        uplift=uplift,
        p_buy_with_offer=p_with,
        cost_if_used_inr=round(cost, 2),
        expected_cost_inr=round(expected_cost, 2),
        value_if_buy_inr=round(value_if_buy, 2),
        expected_profit_inr=round(profit, 2),
        basis=basis,
    )
