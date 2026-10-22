"""Pre-approved message templates (tier T4/T5): what the engine sends when Claude is not used.

One template per offer type and language (English, Hinglish, Punjabi in Gurmukhi). Placeholders are filled
only with facts of the approved offer, so a rendered template always passes the message checks.
"""

import secrets
import string

CATEGORY_LABELS = {
    "Electronics": "electronics",
    "Cell_Phones_and_Accessories": "mobiles & accessories",
    "Clothing_Shoes_and_Jewelry": "fashion",
    "Beauty_and_Personal_Care": "beauty",
    "Home_and_Kitchen": "home & kitchen",
    "Grocery_and_Gourmet_Food": "grocery",
    "Health_and_Household": "health & household",
    "Sports_and_Outdoors": "sports",
    "Toys_and_Games": "toys",
    "Books": "books",
}

TEMPLATES: dict[str, dict[str, str]] = {
    "PCT_OFF": {
        "en": "Hi {name}, enjoy {value}% off {category} on orders above ₹{min_order}. Use code {code} by {expiry}.",
        "hi-Latn": "Namaste {name}! {category} par {value}% ki chhoot, ₹{min_order} se upar ke order par. "
        "Code {code}, {expiry} tak.",
        "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {name} ਜੀ! {category} ਉੱਤੇ {value}% ਦੀ ਛੋਟ, ₹{min_order} ਤੋਂ ਵੱਧ ਦੇ ਆਰਡਰ ਉੱਤੇ। "
        "ਕੋਡ {code}, {expiry} ਤੱਕ।",
    },
    "WIN_BACK": {
        "en": "We miss you, {name}! Here's {value}% off {category} on orders above ₹{min_order}. "
        "Code {code}, valid till {expiry}.",
        "hi-Latn": "{name}, aapki yaad aayi! {category} par {value}% off, ₹{min_order} se upar. "
        "Code {code}, {expiry} tak.",
        "pa": "{name} ਜੀ, ਤੁਹਾਡੀ ਯਾਦ ਆਈ! {category} ਉੱਤੇ {value}% ਛੋਟ, ₹{min_order} ਤੋਂ ਵੱਧ ਉੱਤੇ। "
        "ਕੋਡ {code}, {expiry} ਤੱਕ।",
    },
    "FLAT_OFF": {
        "en": "Hi {name}, get ₹{value} off {category} on orders above ₹{min_order}. Code {code}, valid till {expiry}.",
        "hi-Latn": "Namaste {name}! {category} par ₹{value} off, ₹{min_order} se upar ke order par. "
        "Code {code}, {expiry} tak.",
        "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {name} ਜੀ! {category} ਉੱਤੇ ₹{value} ਦੀ ਛੋਟ, ₹{min_order} ਤੋਂ ਵੱਧ ਦੇ ਆਰਡਰ ਉੱਤੇ। "
        "ਕੋਡ {code}, {expiry} ਤੱਕ।",
    },
    "SECOND_ORDER": {
        "en": "Welcome back, {name}! ₹{value} off your next order above ₹{min_order}. Code {code}, till {expiry}.",
        "hi-Latn": "Welcome {name}! Agle order par ₹{value} off, ₹{min_order} se upar. Code {code}, {expiry} tak.",
        "pa": "ਜੀ ਆਇਆਂ ਨੂੰ {name} ਜੀ! ਅਗਲੇ ਆਰਡਰ ਉੱਤੇ ₹{value} ਛੋਟ, ₹{min_order} ਤੋਂ ਵੱਧ ਉੱਤੇ। ਕੋਡ {code}, {expiry} ਤੱਕ।",
    },
    "UPI_CASHBACK": {
        "en": "Hi {name}, pay by UPI and get ₹{value} cashback on orders above ₹{min_order}. Code {code}, till {expiry}.",
        "hi-Latn": "Namaste {name}! UPI se pay karein aur ₹{value} cashback paayein, ₹{min_order} se upar ke order par. "
        "Code {code}, {expiry} tak.",
        "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {name} ਜੀ! UPI ਨਾਲ ਭੁਗਤਾਨ ਕਰੋ ਤੇ ₹{value} ਕੈਸ਼ਬੈਕ ਪਾਓ, ₹{min_order} ਤੋਂ ਵੱਧ ਉੱਤੇ। "
        "ਕੋਡ {code}, {expiry} ਤੱਕ।",
    },
    "FREE_SHIPPING": {
        "en": "Hi {name}, your next order ships free. Use code {code} by {expiry}.",
        "hi-Latn": "Namaste {name}! Agla order free delivery ke saath. Code {code}, {expiry} tak.",
        "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {name} ਜੀ! ਅਗਲਾ ਆਰਡਰ ਮੁਫ਼ਤ ਡਿਲੀਵਰੀ ਨਾਲ। ਕੋਡ {code}, {expiry} ਤੱਕ।",
    },
    "BONUS_POINTS": {
        "en": "Hi {name}, earn {value}x reward points on your next order. Code {code}, till {expiry}.",
        "hi-Latn": "Namaste {name}! Agle order par {value}x reward points. Code {code}, {expiry} tak.",
        "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {name} ਜੀ! ਅਗਲੇ ਆਰਡਰ ਉੱਤੇ {value}x ਰਿਵਾਰਡ ਪੁਆਇੰਟ। ਕੋਡ {code}, {expiry} ਤੱਕ।",
    },
    "POINTS_EXPIRY": {
        "en": "Hi {name}, you have {points} reward points expiring soon. Use them on your next order.",
        "hi-Latn": "Namaste {name}! Aapke {points} reward points jaldi expire honge. Agle order par use karein.",
        "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {name} ਜੀ! ਤੁਹਾਡੇ {points} ਰਿਵਾਰਡ ਪੁਆਇੰਟ ਜਲਦੀ ਖ਼ਤਮ ਹੋਣਗੇ। ਅਗਲੇ ਆਰਡਰ ਉੱਤੇ ਵਰਤੋ।",
    },
    "CART_NUDGE": {
        "en": "Hi {name}, the items you liked are still waiting for you. Take another look when you're ready.",
        "hi-Latn": "Namaste {name}! Aapke pasand ke items abhi bhi aapka intezaar kar rahe hain.",
        "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {name} ਜੀ! ਤੁਹਾਡੀ ਪਸੰਦ ਦੀਆਂ ਚੀਜ਼ਾਂ ਹਾਲੇ ਵੀ ਤੁਹਾਡੀ ਉਡੀਕ ਕਰ ਰਹੀਆਂ ਹਨ।",
    },
    "EARLY_ACCESS": {
        "en": "Hi {name}, you're invited: early access to our next sale. Code {code}, till {expiry}.",
        "hi-Latn": "Namaste {name}! Agli sale ka early access sirf aapke liye. Code {code}, {expiry} tak.",
        "pa": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ {name} ਜੀ! ਅਗਲੀ ਸੇਲ ਦੀ ਪਹਿਲੀ ਪਹੁੰਚ ਤੁਹਾਡੇ ਲਈ। ਕੋਡ {code}, {expiry} ਤੱਕ।",
    },
}

NO_CODE_TYPES = {"CART_NUDGE", "POINTS_EXPIRY"}


def new_code(offer_id: str) -> str:
    """Unique coupon code with letters only, so it can't be confused with the offer's numbers."""
    return f"OIQ-{offer_id.upper().rstrip('0123456789')[:6]}-" + "".join(
        secrets.choice(string.ascii_uppercase) for _ in range(5)
    )


def _fmt(value: float) -> str:
    return f"{int(value):,}" if float(value).is_integer() else f"{value:g}"


def render(ctx, proposal, template) -> str:
    language = ctx.language if ctx.language in TEMPLATES[template.type] else "en"
    first_name = (ctx.name or "there").split()[0]
    return TEMPLATES[template.type][language].format(
        name=first_name,
        value=_fmt(proposal.value),
        min_order=_fmt(template.min_order_inr),
        category=CATEGORY_LABELS.get(proposal.category or "", "your favourites"),
        code=proposal.code or "",
        expiry=f"{proposal.valid_until.day} {proposal.valid_until:%b}" if proposal.valid_until else "",
        points=_fmt(ctx.points_expiring_30d),
    )
