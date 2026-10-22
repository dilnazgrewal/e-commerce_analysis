"""Make a rules-only offer decision for one or more customers and show every rule check.

Usage (from backend/):
    uv run python ../scripts/decide.py C02588                  # one customer, prints the decision
    uv run python ../scripts/decide.py --sample 200            # summary over 200 random customers
    uv run python ../scripts/decide.py C02588 --save           # also write it to the offer_decisions log
    uv run python ../scripts/decide.py C02588 --now "2026-10-23 11:00"

Needs no API key: this is tiers T4–T6 only.
"""

import argparse
import json
import random
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252

import pandas as pd  # noqa: E402

from app.db import get_engine, init_db  # noqa: E402
from app.rules.context import load_contexts, load_history  # noqa: E402
from app.rules.decide import decide_rules_only  # noqa: E402
from app.rules.effects import OfferEffects  # noqa: E402
from app.rules.schema import load_policy  # noqa: E402


def show(decision) -> None:
    d = decision.to_dict()
    print(f"\n{d['customer_id']}  →  {d['tier']} · {d['status'].upper()}")
    print(f"  why: {d['explanation']['why']}")
    if d["offer"]:
        o = d["offer"]
        print(
            f"  offer: {o['label']} (value {o['value']:g}, category {o['category']}, code {o['code']}, "
            f"until {o['valid_until'][:16]})"
        )
        print(f"  message [{d['message']['lang']}]: {d['message']['text']}")
        e = d["economics"]
        print(
            f"  uplift {e['uplift'] * 100:+.1f} pp (from {e['p_buy_without']:.1%} to {e['p_buy_with']:.1%}); "
            f"cost if used ₹{e['cost_if_used_inr']:,.0f}; expected cost ₹{e['expected_cost_inr']:,.0f}; "
            f"expected profit ₹{e['expected_profit_inr']:,.1f}"
        )
        print(f"  uplift basis: {e['uplift_basis']}")
    print("  checks:")
    for c in d["rule_checks"]:
        print(f"    {'✓' if c['passed'] else '✗'} {c['rule']:26} {c['reason']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("customers", nargs="*")
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--save", action="store_true")
    parser.add_argument(
        "--now",
        type=datetime.fromisoformat,
        default=datetime.now(ZoneInfo("Asia/Kolkata")).replace(tzinfo=None, microsecond=0),
    )
    args = parser.parse_args()

    engine = get_engine()
    init_db(engine)  # creates offer_decisions if missing; never drops
    policy, effects = load_policy(), OfferEffects.from_report()
    ids = args.customers
    if args.sample:
        everyone = pd.read_sql("SELECT customer_id FROM customer_scores", engine)["customer_id"].tolist()
        ids = random.Random(0).sample(everyone, args.sample)
    contexts = load_contexts(engine, ids or None)

    decisions = []
    for cid in ids:
        decision = decide_rules_only(
            contexts[cid], policy, effects, args.now, load_history(engine, cid, args.now)
        )
        decisions.append(decision)
        if not args.sample:
            show(decision)
    if args.save:
        pd.DataFrame([d.to_row() for d in decisions]).to_sql(
            "offer_decisions", engine, if_exists="append", index=False
        )
        print(f"\nSaved {len(decisions)} decision(s) to offer_decisions.")
    if args.sample:
        tiers = Counter(f"{d.tier}/{d.status}" for d in decisions)
        offers = Counter(d.offer["offer_id"] for d in decisions if d.offer)
        sent = [d for d in decisions if d.status == "sent"]
        profit = sum(d.economics["expected_profit_inr"] for d in sent)
        cost = sum(d.economics["expected_cost_inr"] for d in sent)
        print(f"\n{len(decisions)} customers at {args.now:%d %b %H:%M} IST")
        print("  outcome:", dict(tiers.most_common()))
        print("  offers:", dict(offers.most_common()))
        print(f"  sent {len(sent)}: expected cost ₹{cost:,.0f}, expected profit ₹{profit:,.0f}")
        reasons = Counter(d.explanation["why"].split(":")[0][:60] for d in decisions if d.status != "sent")
        print("  not sent:", dict(reasons.most_common(6)))
        langs = Counter(d.message["lang"] for d in sent)
        print("  languages:", dict(langs))
        print(json.dumps(sent[0].to_dict(), ensure_ascii=False, indent=1)[:1500] if sent else "")


if __name__ == "__main__":
    main()
