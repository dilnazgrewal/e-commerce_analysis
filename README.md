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

## Models: who to target, and with what

```bash
cd backend && uv run python ../scripts/train_models.py   # ~5 min → customer_scores table + model_report.json
```

| Model | Method | Result on held-out data |
|---|---|---|
| **Churn** (no order in 60 days) | Gradient boosting on 50+ point-in-time features, with plain-English reason codes (*"No visits in the last 30 days"*) | AUC **0.865**; Brier 0.151 vs 0.249 baseline |
| **Uplift** (extra 14-day purchase chance *because of* an offer) | T-learner per offer type, trained on 3 past randomised campaigns (control + nudge, 2× points, free shipping, 10% off, ₹100 off) | Customers in the model's top 30% gain **1.69×** the average uplift |
| Uplift method check on real data | Same method on the **Hillstrom** RCT (64k real customers) | Top 30% gain **1.35×** (visits), **1.24×** (conversions) |
| **CLV** (90-day) | BG/NBD refitted on the platform's orders × shrunk order value × category margin | P(alive), expected orders, ₹ revenue and ₹ margin |
| **Segments** | Uplift groups (persuadable / sure thing / lost cause / sleeping dog) and lifecycle (new, active, VIP, at-risk, lapsed, window-shopper, bargain-hunter) | Each with a plain-English description |

**Measured offer effects** from the randomised campaigns (extra 14-day purchase rate, 95% CI):

| Offer | Effect |
|---|---|
| Nudge, no discount | +3.8 pp [1.5, 6.1] |
| 2× points | +5.6 pp [3.2, 7.9] |
| Free shipping | +5.5 pp [3.2, 7.8] |
| 10% off | +6.1 pp [3.8, 8.5] |
| ₹100 off | +6.3 pp [4.0, 8.7] |

**Weak spots, reported honestly:**
- **Which offer.** Per-person targeting beats random for 2× points (×2.71) and free shipping (×1.33). It does
  **not** for 10% off (×0.36), ₹100 off (×0.69) or nudges (×1.06): about 1,600 customers per arm is too little.
  The model is reliable for *who* gets an offer.
- **Answer-key check.** The customers are synthetic, so we can also check against the simulator's hidden truth (in
  the script, never in the app). Predicted uplift ranks customers' true response with Spearman 0.17: positive but
  weak, because much of how each person reacts is individual variation.
- **Sleeping dogs.** Customers who react badly to offers (~6%) **cannot** be reliably identified from three
  campaigns. The engine protects them with frequency caps and holdouts rather than trusting that label.
- **Churn calibration.** It is off in the middle range (19% predicted vs 30% observed). CLV has not been backtested yet.

Full numbers: [calibration/model_report.json](calibration/model_report.json) and
[docs/PROJECT_LOG.md](docs/PROJECT_LOG.md) §5.

## Project guide

See [CLAUDE.md](CLAUDE.md) for architecture, rules, conventions, and build progress.
