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

## Data

All customers are **synthetic**. Their behaviour is calibrated on public real-world data — the Amazon Reviews 2023
catalogue and review histories (McAuley Lab), UCI Online Retail II transactions, and the Hillstrom randomised e-mail
experiment — so they behave like real shoppers without exposing any real person.

## Project guide

See [CLAUDE.md](CLAUDE.md) for architecture, rules, conventions, and build progress.
