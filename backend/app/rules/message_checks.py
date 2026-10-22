"""Checks on the text of an offer message (layer L4), so a message can't promise what the offer doesn't give.

The strictest rule is `message_numbers_match`: every number in the text must be a fact of the approved offer
(its value, minimum order, maximum discount, expiry date, points). "₹200 off" on a ₹100 offer fails.
"""

import re
import unicodedata

from app.rules.check import Check

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
LINK = re.compile(r"https?://\S+|www\.\S+|\b[\w-]+\.(?:com|in|net|org|co|io|app|link|ly)\b", re.IGNORECASE)
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
GURMUKHI = re.compile(r"[਀-੿]")
DEVANAGARI = re.compile(r"[ऀ-ॿ]")
EMOJI = re.compile(r"[\U0001F300-\U0001FAFF☀-➿\U0001F000-\U0001F2FF]")


def allowed_numbers(ctx, proposal, template) -> set[float]:
    allowed = {float(proposal.value), float(template.min_order_inr)}
    if template.max_discount_inr is not None:
        allowed.add(float(template.max_discount_inr))
    if proposal.valid_until is not None:
        allowed |= {float(proposal.valid_until.day), float(proposal.valid_until.year)}
    if template.requires_expiring_points and ctx.points_expiring_30d:
        allowed.add(float(ctx.points_expiring_30d))
    return allowed


def _letters(text: str) -> list[str]:
    # Letters plus combining marks: Gurmukhi vowel signs (ਿ ੀ ੇ …) are marks, not letters, in Unicode.
    return [ch for ch in text if unicodedata.category(ch)[0] in ("L", "M")]


def language_ok(text: str, language: str) -> tuple[bool, str]:
    letters = _letters(text)
    if not letters:
        return False, "no words"
    gurmukhi = sum(bool(GURMUKHI.match(ch)) for ch in letters) / len(letters)
    devanagari = sum(bool(DEVANAGARI.match(ch)) for ch in letters) / len(letters)
    if language == "pa":
        return gurmukhi >= 0.4, f"{gurmukhi:.0%} Gurmukhi letters (Punjabi needs ≥ 40%)"
    latin = 1 - gurmukhi - devanagari
    return latin >= 0.8, f"{latin:.0%} Latin letters ({language} needs ≥ 80%)"


def check_message(text: str, ctx, proposal, template, policy) -> list:
    rules = policy.messages
    lowered = text.lower()
    body = text.replace(proposal.code, " ") if proposal.code else text

    found = []
    for token in NUMBER.findall(body):
        try:
            found.append(float(token.replace(",", "")))
        except ValueError:
            continue
    allowed = allowed_numbers(ctx, proposal, template)
    wrong = sorted({f"{n:g}" for n in found if n not in allowed})
    banned = [p for p in rules.banned_phrases if p.lower() in lowered]
    urgency = [p for p in rules.false_urgency_patterns if re.search(p, lowered)]
    competitors = [c for c in rules.competitors if re.search(rf"\b{re.escape(c.lower())}\b", lowered)]
    links = [m for m in LINK.findall(text) if not any(d in m.lower() for d in rules.allowed_link_domains)]
    emojis = len(EMOJI.findall(text))
    lang_ok, lang_reason = language_ok(body, ctx.language)  # the coupon code is not language

    checks = [
        Check("message_not_empty", bool(text.strip()), "ok" if text.strip() else "empty message"),
        Check(
            "message_length",
            len(text) <= rules.max_length,
            f"{len(text)} characters (max {rules.max_length})",
        ),
        Check(
            "message_numbers_match",
            not wrong,
            "every number matches the offer"
            if not wrong
            else f"numbers not in the offer: {', '.join(wrong)}",
        ),
        Check("no_banned_phrases", not banned, "ok" if not banned else f"banned: {banned}"),
        Check("no_false_urgency", not urgency, "ok" if not urgency else f"false urgency: {urgency}"),
        Check("no_competitor_names", not competitors, "ok" if not competitors else f"mentions {competitors}"),
        Check("links_allowed", not links, "ok" if not links else f"links not allowed: {links}"),
        Check(
            "no_contact_details",
            not EMAIL.search(text),
            "ok" if not EMAIL.search(text) else "contains an email",
        ),
        Check("emoji_limit", emojis <= rules.max_emojis, f"{emojis} emojis (max {rules.max_emojis})"),
        Check("language_matches", ctx.language in rules.languages and lang_ok, lang_reason),
    ]
    if proposal.code:
        has_code = proposal.code in text
        checks.append(Check("mentions_code", has_code, "ok" if has_code else f"code {proposal.code} missing"))
    return checks
