# OfferIQ — Personalised Offer Engine

An analysis and decision engine that tells an Indian e-commerce platform **which offer each customer should get,
when, and why**, and delivers those offers through a Telegram bot.

A discount only pays when it changes behaviour. OfferIQ estimates each customer's **uplift** (how much more likely
they are to buy *because of* an offer), picks the cheapest offer likely to work within business rules set by the
marketing team, lets Claude personalise it, checks every AI choice against the rules before sending, and measures the
true incremental impact with holdout groups.

> Status: early build (step 0). Setup and usage below will grow as features land.

## Quick start (local)

Prerequisites: [uv](https://docs.astral.sh/uv/), Node 20+, a Telegram bot token from
[@BotFather](https://t.me/BotFather), an Anthropic API key.

```bash
cp .env.example .env          # then fill in TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY
cd backend
uv sync
uv run uvicorn app.main:app --reload       # API → http://localhost:8000/docs
uv run python -m app.bot.telegram_bot      # bot (polling mode)
uv run pytest
```

```bash
cd frontend
npm install
npm run dev                                # dashboard → http://localhost:5173
```

## Data: synthetic customers, calibrated on real data

All customers are **synthetic**. No real person's data is stored. Their behaviour is calibrated on public
real-world datasets so they act like real shoppers:

| Real source | What we take from it |
|---|---|
| **Amazon Reviews 2023** (McAuley Lab), sampled with HTTP range requests across 10 categories | A catalogue of ~15k real products (titles, brands, images, ratings, popularity), prices converted to ₹ price points; category volumes; real per-user purchase-gap distributions |
| **UCI Online Retail II** (1.07M real transactions) | A **BG/NBD** model fitted by maximum likelihood: how purchase rates and churn vary between customers |
| **Hillstrom e-mail experiment** (64k customers, randomised) | Real causal offer effects by recency × spend history, which set how strongly each synthetic customer responds to an offer |
| `calibration/india.yaml` | Editable Indian-market assumptions: city tiers, COD/UPI mix, RTO, margins, festive calendar, language by state |

```bash
cd backend
uv run python ../scripts/bootstrap_data.py   # sample real data → calibration/ (~300 MB download, once)
uv run python ../scripts/seed_db.py          # 5,000 customers + 12-month event log → DB + realism report
```

`calibration/realism_report.json` compares the synthetic data with the real data. It checks the shape of purchase
gaps (KS test against UCI and Amazon), repeat rate, category mix, price distribution, cart abandonment, conversion and
COD returns. The tests fail if any check drifts past its threshold.

## Project guide

See [CLAUDE.md](CLAUDE.md) for architecture, rules, conventions, and build progress.
