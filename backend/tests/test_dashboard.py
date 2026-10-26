"""Dashboard API, versioned policies, Claude rule editing (fake Claude) and the Policy Lab."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

from app import models  # noqa: F401  (registers tables)
from app.agent import rule_author
from app.agent.harness import Harness
from app.api import dashboard
from app.bot.service import Services
from app.main import app
from app.rules import policy_store
from app.rules.schema import load_policy
from tests.agent_fakes import FakeLLM, text_reply

POLICY = load_policy()
DB = Path(__file__).resolve().parents[1] / "offeriq.db"


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'dash.db'}")
    SQLModel.metadata.create_all(eng)
    return eng


def as_dict(policy) -> dict:
    return json.loads(policy.model_dump_json())


def edited(**changes) -> dict:
    p = as_dict(POLICY)
    p["categories"]["Electronics"]["max_discount_pct"] = changes.get("electronics", 5)
    p["budget"]["daily_inr"] = changes.get("daily", 50000)
    return p


# --------------------------------------------------------------------------- policy store


def test_default_file_is_live_until_a_version_is_published(engine):
    assert policy_store.active(engine).version == POLICY.version
    assert policy_store.history(engine) == []


def test_publish_appends_a_version_and_the_bot_uses_it(engine):
    services = Services(engine, POLICY, None, None)
    published = policy_store.publish(engine, edited(electronics=3), "Asha", "Lower electronics")
    assert published.version == POLICY.version + 1 and published.updated_by == "Asha"
    assert services.policy.categories["Electronics"].max_discount_pct == 3
    second = policy_store.publish(engine, edited(electronics=4), "Asha", "Again")
    assert second.version == POLICY.version + 2
    assert [h["version"] for h in policy_store.history(engine)] == [second.version, published.version]


def test_publish_rejects_an_invalid_policy(engine):
    bad = edited()
    bad["budget"]["daily_inr"] = -1
    with pytest.raises(ValueError):
        policy_store.publish(engine, bad, "Asha", "")
    assert policy_store.history(engine) == []


def test_diff_lists_exactly_the_changed_values():
    changes = policy_store.diff(as_dict(POLICY), edited(electronics=3, daily=20000))
    assert {c["path"] for c in changes} == {"categories.Electronics.max_discount_pct", "budget.daily_inr"}
    offers = as_dict(POLICY)
    offers["offers"][1]["value"]["max"] = 3
    assert policy_store.diff(as_dict(POLICY), offers)[0]["path"] == "offers[points2x].value.max"


# --------------------------------------------------------------------------- Claude rule author (fake)


def reply(policy: dict, understood=True, summary="Electronics capped at 3%.", warnings=()):
    payload = {
        "understood": understood,
        "summary": summary,
        "policy_json": json.dumps(policy),
        "warnings": list(warnings),
    }
    return text_reply(json.dumps(payload))


def harness_with(*steps) -> tuple[Harness, FakeLLM]:
    fake = FakeLLM(*steps)
    return Harness(opus=fake), fake


def test_rule_author_returns_a_validated_proposal():
    proposal_policy = edited(electronics=3)
    proposal_policy["version"] = 99  # Claude may not set the version: the system does on publish
    h, fake = harness_with(reply(proposal_policy))
    out = rule_author.propose(h, POLICY, "cap electronics at 3%", {"Electronics": 8})
    assert out.ok and out.attempts == 1
    assert out.policy["categories"]["Electronics"]["max_discount_pct"] == 3
    assert out.policy["version"] == POLICY.version
    request = fake.requests[0]
    assert request["model"] == "claude-opus-5-5" and request["fallbacks"] == "default"
    assert "<request>" in request["messages"][0]["content"][1]["text"]


def test_rule_author_retries_once_with_the_validation_errors():
    bad = edited()
    bad["budget"]["surprise_key"] = 1
    h, fake = harness_with(reply(bad), reply(edited(electronics=3)))
    out = rule_author.propose(h, POLICY, "cap electronics at 3%", {})
    assert out.ok and out.attempts == 2
    assert "surprise_key" in fake.requests[1]["messages"][-1]["content"]


def test_rule_author_gives_up_after_two_invalid_replies():
    h, _ = harness_with(text_reply("not json"), text_reply("still not json"))
    out = rule_author.propose(h, POLICY, "make it better", {})
    assert not out.ok and out.policy is None and out.attempts == 2


def test_rule_author_reports_requests_it_cannot_apply():
    h, _ = harness_with(reply(edited(), understood=False, summary="There is no rule for delivery speed."))
    out = rule_author.propose(h, POLICY, "deliver faster", {})
    assert not out.ok and "delivery speed" in out.errors[0]


def test_rule_author_respects_the_kill_switch(monkeypatch):
    monkeypatch.setenv("OFFERIQ_AI_ENABLED", "0")
    h, fake = harness_with()
    out = rule_author.propose(h, POLICY, "cap electronics", {})
    assert not out.ok and "kill switch" in out.errors[0] and fake.requests == []


# --------------------------------------------------------------------------- API


@pytest.fixture
def api(engine, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "correct horse")
    monkeypatch.setattr(dashboard, "get_engine", lambda: engine)
    monkeypatch.setattr(dashboard.time, "sleep", lambda s: None)
    from app.config import get_settings

    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


def test_dashboard_needs_the_password(api):
    assert api.get("/api/policy").status_code == 401
    assert api.post("/api/login", json={"password": "wrong"}).status_code == 401
    token = api.post("/api/login", json={"password": "correct horse"}).json()["token"]
    assert "correct horse" not in token
    assert api.get("/api/policy", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert api.get("/api/policy", headers={"Authorization": "Bearer forged"}).status_code == 401


def test_rules_studio_check_then_publish(api):
    headers = {
        "Authorization": "Bearer "
        + api.post("/api/login", json={"password": "correct horse"}).json()["token"]
    }
    risky = edited(electronics=30)
    check = api.post("/api/policy/check", json={"policy": risky}, headers=headers).json()
    assert check["ok"] and any("Electronics" in w for w in check["warnings"])
    bad = edited()
    bad["offers"][0]["type"] = "FREE_MONEY"
    assert not api.post("/api/policy/check", json={"policy": bad}, headers=headers).json()["ok"]
    assert api.post("/api/policy/publish", json={"policy": bad}, headers=headers).status_code == 422
    assert (
        api.post("/api/policy/publish", json={"policy": as_dict(POLICY)}, headers=headers).status_code == 400
    )
    done = api.post(
        "/api/policy/publish", json={"policy": edited(electronics=3), "note": "t"}, headers=headers
    )
    assert done.json() == {"version": POLICY.version + 1, "changes": 1}
    live = api.get("/api/policy", headers=headers).json()
    assert live["policy"]["categories"]["Electronics"]["max_discount_pct"] == 3
    assert live["history"][0]["note"] == "t"


def test_overview_and_decision_log_work_on_an_empty_database(api):
    headers = {
        "Authorization": "Bearer "
        + api.post("/api/login", json={"password": "correct horse"}).json()["token"]
    }
    overview = api.get("/api/overview", headers=headers).json()
    assert overview["decisions"] == 0 and overview["rule_violations_in_sent"] == 0
    assert api.get("/api/decisions", headers=headers).json() == {"total": 0, "items": []}
    assert api.get("/api/decisions/D-missing", headers=headers).status_code == 404


# --------------------------------------------------------------------------- Policy Lab (needs the seeded DB)

needs_db = pytest.mark.skipif(not DB.exists(), reason="run scripts/seed_db.py and train_models.py first")


@pytest.fixture(scope="module")
def lab_world():
    from app.db import make_engine
    from app.experiments import policy_lab

    eng = make_engine(f"sqlite:///{DB}")
    return eng, policy_lab.world(eng, sample=600, seed=3)


@needs_db
def test_expected_values_match_the_dice_simulator(lab_world):
    from app.experiments import policy_lab
    from app.rules.effects import OfferEffects

    _, w = lab_world
    for strategy in ("none", "blanket20", "offeriq"):
        offers = policy_lab.choose_offers(strategy, w, POLICY, OfferEffects.from_report())
        expected = policy_lab.expected_outcomes(w, offers, 14, {})["orders"].sum()
        simulated = policy_lab.simulate_check(w, offers, 14, seeds=(1, 2, 3, 4))
        assert abs(expected - simulated) / simulated < 0.06, (strategy, expected, simulated)


@needs_db
def test_offeriq_beats_blanket_discounts_and_reports_fairness(lab_world):
    from app.experiments import policy_lab
    from app.rules.effects import OfferEffects

    eng, _ = lab_world
    out = policy_lab.compare(eng, POLICY, OfferEffects.from_report(), list(policy_lab.STRATEGIES), sample=600)
    by = {r["strategy"]: r for r in out["results"]}
    assert by["none"]["extra_profit_inr"] == 0
    assert by["offeriq"]["extra_profit_inr"] > by["blanket20"]["extra_profit_inr"]
    assert by["offeriq"]["wasted_discount_inr"] < by["blanket20"]["wasted_discount_inr"]
    assert set(out["fairness"]) == {"city_tier", "gender", "age_band"}
    stronger = policy_lab.compare(
        eng, POLICY, OfferEffects.from_report(), ["blanket20"], discount_strength=2.0, sample=600
    )
    blanket_2x = next(r for r in stronger["results"] if r["strategy"] == "blanket20")
    assert blanket_2x["extra_orders"] > by["blanket20"]["extra_orders"]
