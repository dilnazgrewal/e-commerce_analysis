"""Layer L0: anything a customer typed is untrusted data, never instructions.

`clean()` strips control characters, invisible Unicode and markup, and caps the length.
`injection_signals()` flags known prompt-injection shapes so the decision log can show them. Even unflagged
text reaches Claude only inside <customer_data> tags, and nothing Claude proposes is sent without passing
the rules, so a successful injection can at worst change the *wording* of an allowed offer, never its value.
"""

import re
import unicodedata

MAX_FIELD_CHARS = 300

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)(\s+\w+)?\s+(instructions|rules|prompts?)",
    r"ignore\s+(all\s+|the\s+)?(rules|instructions|policy|the\s+above)",
    r"disregard\s+(all\s+|the\s+)?(rules|instructions|policy)",
    r"forget\s+(your|all|the)\s+(rules|instructions)",
    r"\b(system|developer|admin(istrator)?)\s+(prompt|message|mode|override)\b",
    r"(show|repeat|print|reveal|list)\s+(me\s+)?(your|the)\s+(system\s+prompt|instructions|rules)",
    r"what'?s\s+in\s+your\s+(instructions|prompt)",
    r"you\s+are\s+(now|no\s+longer)\b",
    r"\bact\s+as\b|pretend\s+(to\s+be|you\s+are)",
    r"\bjailbreak\b|\bDAN\b",
    r"(give|send|apply|grant|approve)\s+(me\s+|us\s+)?(a\s+|an\s+)?\d{2,3}\s?(%|percent)",
    r"\b(100|[5-9]\d)\s?(%|percent)\s*(off|discount|chhoot|ਛੋਟ)",
    r"₹\s?\d{4,}\s*(coupon|cashback|off)",
    r"new\s+(instructions|rules|policy)\s*:",
    r"</?\s*(system|instructions?|customer_data|tool|assistant)\s*>",
    r"\bi\s+am\s+(the\s+|an\s+)?(owner|admin|manager|developer|ceo)\b|\bmain\s+admin\s+hoon\b",
    r"(rules|niyam|policy|instructions)\s+(bhool|ignore|tod)",
    r"(saare|sab)\s+(rules|niyam)\s+bhool",
    r"system\s+prompt\s+dikha",
    r"ਨਿਯਮ\s+(ਭੁੱਲ|ਤੋੜ|ਨਜ਼ਰਅੰਦਾਜ਼)",
    r"submit_decision|precheck_offer|tool_?call",
]
_COMPILED = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]
_INVISIBLE = {"Cf", "Cc", "Co", "Cs"}


def clean(text: str | None, limit: int = MAX_FIELD_CHARS) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text))
    text = "".join(ch if ch in "\n\t" or unicodedata.category(ch) not in _INVISIBLE else " " for ch in text)
    text = re.sub(r"[<>{}`\\]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def injection_signals(text: str | None) -> list[str]:
    if not text:
        return []
    # NFKC folds full-width/compat letters; invisible format characters (zero-width spaces etc.) are
    # deleted rather than spaced, so they can't split a keyword.
    normalised = unicodedata.normalize("NFKC", str(text))
    normalised = "".join(c for c in normalised if c.isspace() or unicodedata.category(c) not in _INVISIBLE)
    normalised = re.sub(r"\s+", " ", normalised)
    return [p.pattern for p in _COMPILED if p.search(normalised)]


def guard_fields(fields: dict[str, str | None]) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Clean every untrusted field; return (cleaned fields, {field: injection signals})."""
    cleaned, flags = {}, {}
    for key, value in fields.items():
        signals = injection_signals(value)
        if signals:
            flags[key] = signals
        cleaned[key] = clean(value)
    return cleaned, flags
