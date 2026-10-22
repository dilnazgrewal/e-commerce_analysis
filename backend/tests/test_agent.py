"""Guardrail tests with a scripted fake Claude: each misbehaviour is caught by the right layer, and the
decision steps down to the right tier. Nothing here calls the real API."""

import json

import pytest

from app.agent.harness import CircuitBreaker, Harness, TokenBudget
from app.agent.pipeline import decide
from app.rules.engine import History
from tests.agent_fakes import (
    FakeLLM,
    best_offer_submission,
    good_message,
    haiku_good,
    menu_from,
    rate_limited,
    server_error,
    submit,
    text_reply,
    timeout,
    tool_call,
)
from tests.rules_helpers import NOW, POLICY, ctx, effects

E = effects()


def run(sonnet: FakeLLM, haiku: FakeLLM | None = None, customer=None, **kw):
    harness = Harness(
        sonnet=sonnet,
        haiku=haiku or FakeLLM(),
        budget=kw.pop("budget", None),
        breaker=kw.pop("breaker", None),
    )
    return decide(
        customer or ctx(), POLICY, E, NOW, History(), harness, activity={"recent_views": []}, **kw
    ), harness


def first_menu(sonnet: FakeLLM) -> list[dict]:
    return menu_from(sonnet.requests[0])


def test_t1_happy_path_sends_claudes_choice():
    sonnet = FakeLLM(best_offer_submission)
    d, _ = run(sonnet)
    assert d.status == "sent" and d.tier == "T1"
    assert d.ai["used"] and len(d.ai["calls"]) == 1 and d.ai["calls"][0]["model"] == "claude-sonnet-5-5"
    assert all(c["passed"] for c in d.rule_checks)
    req = sonnet.requests[0]
    assert req["fallbacks"] == "default" and req["output_config"] == {"effort": "low"}
    assert "tool_choice" not in req  # forced tool use is a 400 on Sonnet 5.5
    assert all(t["strict"] for t in req["tools"])


def test_claude_only_sees_offers_the_rules_allow():
    sonnet = FakeLLM(best_offer_submission)
    run(sonnet, customer=ctx(uplift_group="sure_thing"))
    offered = {o["type"] for o in first_menu(sonnet)}
    assert offered and offered <= {"BONUS_POINTS", "EARLY_ACCESS", "POINTS_EXPIRY", "CART_NUDGE"}


@pytest.mark.parametrize(
    ("bad", "layer_hint"),
    [
        (lambda m: submit("pct10", 90, good_message(m)), "value_in_range"),  # 90% off
        (lambda m: submit("mega90", 90, "Hi Simran, 90% off!"), "offer_on_menu"),  # not on the menu
        (
            lambda m: submit(m["offer_id"], m["value"], "Hi Simran, ₹500 off everything today!"),
            "message_numbers_match",
        ),
        (lambda m: submit(m["offer_id"], m["value"], good_message(m) + " Only 2 left!"), "no_false_urgency"),
        (
            lambda m: submit(m["offer_id"], m["value"], good_message(m) + " Cheaper than Flipkart."),
            "no_competitor",
        ),
        (lambda m: submit(m["offer_id"], m["value"], "Hi"), None),  # missing coupon code (if any) or fine
    ],
)
def test_bad_first_submission_is_rejected_then_t2_fixes_it(bad, layer_hint):
    def first(kwargs):
        return bad(menu_from(kwargs)[0])

    def second(kwargs):
        offer = menu_from(kwargs)[0]
        return submit(offer["offer_id"], offer["value"], good_message(offer), call_id="toolu_2")

    sonnet = FakeLLM(first, second)
    d, _ = run(sonnet)
    assert d.status == "sent"
    if layer_hint is None and d.tier == "T1":
        return  # "Hi" is a valid message for an offer without a code
    assert d.tier == "T2"
    assert layer_hint is None or any(layer_hint in r for r in d.ai["rejections"][0])
    # the rejection reasons went back to Claude as an error tool_result
    retry_msgs = sonnet.requests[1]["messages"]
    last = retry_msgs[-1]["content"][0]
    assert (
        last["type"] == "tool_result"
        and last["is_error"]
        and "Rejected by the business rules" in last["content"]
    )


def test_two_rejections_fall_to_haiku_t3():
    bad = lambda kw: submit("pct10", 90, "90% off!!")  # noqa: E731
    d, h = run(FakeLLM(bad, bad), FakeLLM(haiku_good))
    assert d.status == "sent" and d.tier == "T3"
    assert [c["tier"] for c in d.ai["calls"]] == ["T1", "T2", "T3"]
    assert d.ai["calls"][-1]["model"] == "claude-haiku-5-5"


def test_everything_fails_falls_to_rules_only():
    bad = lambda kw: submit("pct10", 90, "90% off!!")  # noqa: E731
    lies = json.dumps({"offer_id": "nudge", "message": "Only 2 left, ₹999 off!", "reason": "x"})
    d, _ = run(FakeLLM(bad, bad), FakeLLM(text_reply(lies)))
    assert d.tier in {"T4", "T5", "T6"} and d.ai["fallback"] == "rules-only"
    if d.status == "sent":
        assert all(c["passed"] for c in d.rule_checks)


@pytest.mark.parametrize("failure", [timeout(), server_error(), rate_limited()])
def test_api_errors_step_down_to_t3(failure):
    d, harness = run(FakeLLM(failure), FakeLLM(haiku_good))
    assert d.tier == "T3" and d.status == "sent"
    assert d.ai["calls"][0]["ok"] is False and d.ai["t1_t2_error"] in {
        "timeout",
        "server_error",
        "rate_limited",
    }


def test_invalid_haiku_json_and_no_tool_call_fall_to_rules_only():
    d, _ = run(FakeLLM(text_reply("I think free delivery is best.")), FakeLLM(text_reply("not json")))
    assert d.ai["t1_t2_error"].startswith("no tool call") and d.ai["fallback"] == "rules-only"
    assert d.tier in {"T4", "T5", "T6"}


def test_refusal_is_a_failure_not_a_decision():
    refusal = text_reply("", stop="refusal")
    d, _ = run(FakeLLM(refusal), FakeLLM(haiku_good))
    assert d.ai["t1_t2_error"] == "refusal" and d.tier == "T3"


def test_precheck_then_submit_uses_the_rules_engine():
    def precheck(kwargs):
        offer = menu_from(kwargs)[0]
        return tool_call(
            "precheck_offer",
            {"offer_id": offer["offer_id"], "value": offer["value"], "message": "Only 2 left!"},
            "toolu_p",
        )

    sonnet = FakeLLM(precheck, best_offer_submission)
    d, _ = run(sonnet)
    feedback = sonnet.requests[1]["messages"][-1]["content"][0]["content"]
    assert feedback.startswith("Fails:") and "no_false_urgency" in feedback
    assert d.tier == "T1" and d.status == "sent"  # precheck doesn't count as a submission


def test_claude_may_choose_no_offer():
    d, _ = run(FakeLLM(submit("none", 0, "", "Customer just bought; nothing would add value.")))
    assert d.status == "suppressed" and d.tier == "T1" and d.offer is None
    assert "Claude chose not to send" in d.explanation["why"]


def test_tool_call_budget_is_enforced():
    loop = lambda kw: tool_call("get_recent_activity", {}, "toolu_a")  # noqa: E731
    d, _ = run(FakeLLM(*[loop] * 6), FakeLLM(haiku_good))
    assert d.tier == "T3" and "tool-call budget" in d.ai["t1_t2_error"]


def test_kill_switch_budget_and_breaker_skip_ai(monkeypatch):
    monkeypatch.setenv("OFFERIQ_AI_ENABLED", "0")
    d, _ = run(FakeLLM())
    assert d.ai == {"used": False, "skipped": "kill switch is on (AI disabled)"} and d.tier in {
        "T4",
        "T5",
        "T6",
    }
    monkeypatch.delenv("OFFERIQ_AI_ENABLED")

    d, _ = run(FakeLLM(), budget=TokenBudget(daily_limit=1000, used=1000))
    assert "token budget" in d.ai["skipped"]

    clock = [0.0]
    breaker = CircuitBreaker(failures=2, window_s=60, cooldown_s=300, clock=lambda: clock[0])
    run(FakeLLM(server_error()), FakeLLM(server_error()), breaker=breaker)  # 2 failures → open
    d, _ = run(FakeLLM(), breaker=breaker)
    assert "circuit breaker" in d.ai["skipped"]
    clock[0] = 301
    d, _ = run(FakeLLM(best_offer_submission), breaker=breaker)
    assert d.tier == "T1"  # closed again after the cool-down


def test_contact_rules_block_before_any_ai_call():
    sonnet = FakeLLM()
    d, _ = run(sonnet, customer=ctx(opted_out=True))
    assert sonnet.requests == [] and d.status == "suppressed" and d.ai["used"] is False
