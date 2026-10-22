"""Message checks (layer L4): a message must not promise anything the approved offer doesn't give."""

from datetime import timedelta

import pytest

from app.rules.engine import Proposal
from app.rules.message_checks import check_message
from app.rules.templates import TEMPLATES, render
from tests.rules_helpers import NOW, POLICY, ctx

PCT = POLICY.offer("pct10")
P = Proposal("pct10", 10, "Clothing_Shoes_and_Jewelry", NOW + timedelta(days=7), "OIQ-PCT-ABCDE")


def failures(text, customer=None, proposal=P, template=PCT):
    return {
        c.rule for c in check_message(text, customer or ctx(), proposal, template, POLICY) if not c.passed
    }


def test_good_message_passes():
    text = "Hi Simran, enjoy 10% off fashion on orders above ₹499. Use code OIQ-PCT-ABCDE by 29 Oct."
    assert failures(text) == set()


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        ("Hi Simran, enjoy 20% off fashion! Code OIQ-PCT-ABCDE", "message_numbers_match"),
        ("Hi Simran, ₹500 off today! Code OIQ-PCT-ABCDE", "message_numbers_match"),
        ("Only 2 left! 10% off. Code OIQ-PCT-ABCDE", "no_false_urgency"),
        ("Selling fast: 10% off. Code OIQ-PCT-ABCDE", "no_false_urgency"),
        ("Guaranteed lowest price, 10% off. Code OIQ-PCT-ABCDE", "no_banned_phrases"),
        ("Cheaper than Flipkart: 10% off. Code OIQ-PCT-ABCDE", "no_competitor_names"),
        ("10% off at https://evil.example.com code OIQ-PCT-ABCDE", "links_allowed"),
        ("10% off, reply to deals@shop.in. Code OIQ-PCT-ABCDE", "no_contact_details"),
        ("10% off 🎉🎉🎉🎉 Code OIQ-PCT-ABCDE", "emoji_limit"),
        ("Hi Simran, enjoy 10% off fashion on orders above ₹499.", "mentions_code"),
        ("x" * 400 + " OIQ-PCT-ABCDE", "message_length"),
        ("   ", "message_not_empty"),
    ],
)
def test_each_message_rule(text, rule):
    assert rule in failures(text)


def test_language_must_match_preference():
    hinglish = "Namaste Simran! fashion par 10% ki chhoot. Code OIQ-PCT-ABCDE"
    assert "language_matches" in failures(hinglish, ctx(language="pa"))
    punjabi = "ਸਤ ਸ੍ਰੀ ਅਕਾਲ Simran ਜੀ! fashion ਉੱਤੇ 10% ਦੀ ਛੋਟ। ਕੋਡ OIQ-PCT-ABCDE"
    assert "language_matches" not in failures(punjabi, ctx(language="pa"))
    assert "language_matches" in failures("नमस्ते Simran! 10% छूट OIQ-PCT-ABCDE", ctx(language="hi-Latn"))


def test_coupon_code_digits_never_count_as_numbers():
    assert failures("10% off with OIQ-PCT-ABCDE", proposal=P) == set()


@pytest.mark.parametrize("offer", [o.id for o in POLICY.offers])
@pytest.mark.parametrize("language", ["en", "hi-Latn", "pa"])
def test_every_template_passes_the_message_checks(offer, language):
    template = POLICY.offer(offer)
    assert set(TEMPLATES[template.type]) == {"en", "hi-Latn", "pa"}
    customer = ctx(language=language, points_expiring_30d=1240)
    category = (
        "Clothing_Shoes_and_Jewelry"
        if template.type in {"PCT_OFF", "WIN_BACK", "FLAT_OFF", "SECOND_ORDER", "UPI_CASHBACK"}
        else None
    )
    code = None if template.type in {"CART_NUDGE", "POINTS_EXPIRY"} else "OIQ-TEST-ABCDE"
    proposal = Proposal(
        offer, template.value.default, category, NOW + timedelta(days=template.validity_days), code
    )
    text = render(customer, proposal, template)
    assert failures(text, customer, proposal, template) == set(), text
