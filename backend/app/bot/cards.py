"""What the bot shows: quiz questions, the twin card, product cards, offer cards and the "Why me?" answer."""

import json
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.rules.templates import CATEGORY_LABELS

LANGUAGES = [("en", "English"), ("hi-Latn", "Hinglish"), ("pa", "ਪੰਜਾਬੀ")]
QUIZ = [
    ("language", "Which language should your offers be in?", LANGUAGES),
    (
        "tier",
        "Where do you live?",
        [
            ("metro", "🏙 Metro (Delhi, Mumbai…)"),
            ("city", "🏘 Big city (Ludhiana, Jaipur…)"),
            ("town", "🌾 Smaller town (Bathinda, Moga…)"),
        ],
    ),
    (
        "category",
        "What do you shop for most?",
        [
            ("fashion", "👗 Fashion"),
            ("electronics", "📱 Phones & electronics"),
            ("beauty", "💄 Beauty & health"),
            ("home", "🏠 Home & grocery"),
            ("books", "📚 Books & toys"),
            ("sports", "⚽ Sports"),
        ],
    ),
    (
        "frequency",
        "How often do you order online?",
        [
            ("weekly", "Every week"),
            ("monthly", "A few times a month"),
            ("quarterly", "Every few months"),
            ("rarely", "Rarely"),
        ],
    ),
    (
        "budget",
        "What do you usually spend per order?",
        [("low", "Under ₹500"), ("mid", "₹500–1,500"), ("high", "₹1,500–5,000"), ("premium", "₹5,000+")],
    ),
]
BROWSE = [(key, label) for key, label in QUIZ[2][2]]
BUDGET_CAP = {"low": 800, "mid": 2500, "high": 8000, "premium": None}


def keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(text, callback_data=data) for data, text in row] for row in rows]
    )


def quiz_question(step: int) -> tuple[str, InlineKeyboardMarkup]:
    field, question, options = QUIZ[step]
    rows = [[(f"q:{field}:{key}", label)] for key, label in options]
    return f"<b>{step + 1}/{len(QUIZ)}</b> {question}", keyboard(rows)


def inr(value) -> str:
    return f"₹{int(round(float(value))):,}"


def twin_text(first_name: str, twin: dict) -> str:
    cats = " + ".join(CATEGORY_LABELS.get(c, c) for c in twin["categories"]) or "a bit of everything"
    last = (
        f"last order {twin['last_order_days']} days ago"
        if twin["last_order_days"] is not None
        else "no orders yet"
    )
    return (
        f"🧬 <b>Meet your shopping twin, {escape(first_name)}!</b>\n\n"
        f"You shop like <b>{escape(twin['name'])} from {escape(twin['city'])}</b>: {twin['tier']} city, "
        f"loves {escape(cats)}, {twin['orders_365d']} orders in the past year (usually {inr(twin['aov_inr'])}), "
        f"{twin['points']:,} reward points, {last}.\n\n"
        f"From now on their 12-month (simulated) history is yours, and OfferIQ decides your offers from it."
    )


def product_caption(p: dict) -> str:
    mrp = f" <s>{inr(p['mrp_inr'])}</s>" if p["mrp_inr"] and p["mrp_inr"] > p["price_inr"] else ""
    rating = f"⭐ {p['rating']:.1f} ({int(p['rating_count']):,})" if p.get("rating") else ""
    return f"<b>{escape(str(p['title'])[:110])}</b>\n{inr(p['price_inr'])}{mrp}  {rating}\n<i>{escape(str(p['brand']))}</i>"


def product_buttons(product_id: str) -> InlineKeyboardMarkup:
    return keyboard([[(f"cart:{product_id}", "❤️ Add to cart")]])


def offer_text(d: dict) -> str:
    offer, message = d["offer"], d["message"]
    details = [f"🎟 <b>{escape(offer['label'])}</b>"]
    if offer.get("code"):
        details.append(f"code <code>{escape(offer['code'])}</code>")
    if offer.get("valid_until"):
        valid = offer["valid_until"]
        valid = valid if isinstance(valid, str) else valid.isoformat()
        details.append(f"valid till {valid[8:10]}/{valid[5:7]}")
    return (
        f"{escape(message['text'])}\n\n{' · '.join(details)}\n<i>Simulated offer · OfferIQ hackathon demo</i>"
    )


def offer_buttons(decision_id: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [(f"claim:{decision_id}", "🛒 Claim"), (f"nope:{decision_id}", "🙅 Not for me")],
            [(f"why:{decision_id}", "❓ Why me?")],
        ]
    )


def reason_buttons(decision_id: str) -> InlineKeyboardMarkup:
    return keyboard(
        [
            [
                (f"rsn:{decision_id}:price", "Still too expensive"),
                (f"rsn:{decision_id}:product", "Not interested"),
            ],
            [
                (f"rsn:{decision_id}:bought", "Already bought it"),
                (f"rsn:{decision_id}:spam", "Too many offers"),
            ],
        ]
    )


def why_text(row: dict) -> str:
    """Customer-facing explanation: why this offer, what it's based on, and which rules it passed."""
    explanation = json.loads(row["explanation"])
    checks = json.loads(row["rule_checks"])
    ai = json.loads(row["ai"]) if row.get("ai") else {}
    passed = [c for c in checks if c["passed"]]
    chooser = (
        "Claude (an AI), then checked by OfferIQ's business rules"
        if ai.get("used") and row["tier"] in ("T1", "T2", "T3")
        else "OfferIQ's business rules (no AI for this one)"
    )
    lines = [
        "<b>Why you got this offer</b>",
        f"• {escape(explanation.get('why', ''))}",
    ]
    if row.get("uplift") is not None:
        lines.append(
            f"• Our models expect it to raise your chance of shopping this week by about "
            f"{float(row['uplift']) * 100:.0f} percentage points, based on randomised tests with similar shoppers."
        )
    lines.append(f"• Chosen by: {chooser}.")
    highlights = {
        "frequency_cap_7d": "at most 2 offers a week",
        "quiet_hours": "never at night (9 pm–9 am)",
        "category_discount_cap": "discount within the category limit",
        "not_opted_out": "you haven't opted out",
        "message_numbers_match": "every number in the message matches the real offer",
        "no_false_urgency": "no fake urgency",
        "daily_budget": "within today's offer budget",
    }
    shown = [text for rule, text in highlights.items() if any(c["rule"] == rule for c in passed)]
    lines.append(f"• Passed {len(passed)} of {len(checks)} rule checks, including: {', '.join(shown)}.")
    lines.append("\nType /stop any time to stop offers.")
    return "\n".join(lines)


def history_text(rows: list[dict]) -> str:
    if not rows:
        return "No offers yet. Try /browse or /offer."
    icons = {
        "sent": "📩",
        "claimed": "✅",
        "dismissed": "🙅",
        "suppressed": "⏸",
        "queued": "🌙",
        "holdout": "🧪",
    }
    lines = ["<b>Your recent offers</b>"]
    for r in rows:
        what = f"{r['offer_type'].replace('_', ' ').lower()}" if r["offer_type"] else "no offer"
        lines.append(
            f"{icons.get(r['status'], '•')} {str(r['created_at'])[:16]} · {what} · {r['status']} ({r['tier']})"
        )
    return "\n".join(lines)
