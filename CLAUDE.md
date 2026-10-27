# CLAUDE.md — OfferIQ

Personalised Offer Engine for Indian e-commerce (Hackathon 2 — Punjab Jobs AI Bootcamp × J-PAL South Asia).
OfferIQ is **not a store**. It is an analysis + decision service a platform plugs into: the platform sends customer
events in; OfferIQ decides *who gets which offer, when, and why*, delivers it (via Telegram for the hackathon), and
measures whether the offer actually changed behaviour (uplift), not just who bought.

Deadline: **Tue 27 Oct 2026, 12:00 IST**. Submission = public GitHub repo (README + this file) + Telegram bot username.

## Non-negotiable rules

1. **Every AI choice passes `rules.validate` before it is sent.** Nothing reaches Telegram without a passing verdict.
   Claude only ever sees the menu of offers the rules already allow (L1), its output is schema-checked (L2),
   rule-checked (L3), message-checked (L4) and re-checked at send time inside a DB transaction (L5).
2. **Fallback chain must always produce a safe outcome**: T1 Claude agent → T2 retry with rejection reasons →
   T3 Haiku single call over rules' top-3 → T4 rules-only next-best-offer + approved templates →
   T5 marketer-set segment fallback → T6 no offer (logged "suppressed"). T4–T6 use no AI.
3. **Synthetic customers only.** Real public datasets (Amazon Reviews 2023, UCI Online Retail II, Hillstrom) are
   used only to calibrate distributions and as the product catalogue. `data/raw/` is never committed; only small
   derived files in `calibration/` are.
4. **Models never read `sim_hidden_traits`** (the simulator's ground truth). It is used only for scoring policies.
5. **Secrets live only in `.env`** (`TELEGRAM_BOT_TOKEN`, `ANTHROPIC_API_KEY`, `DATABASE_URL`, `WEBHOOK_URL`, …).
   Never log, print, commit, or paste them. `.env.example` lists the keys with empty values.
6. **Claude never pushes.** Make small commits after each working step; the user pushes.
7. Claude (the runtime agent) never changes rules without a human Approve click, and never does ₹ maths or scoring —
   that is deterministic code.

## Architecture

```
Simulator ("platform") ─events─▶ FastAPI backend ◀─policy edits─ React dashboard (marketing team)
Telegram bot (classmates) ─clicks/text─▶   │
                         ◀─offer cards──   ├ event store → features → churn · uplift · CLV · segments
                                           ├ trigger detector (cart abandoned, drifting, points expiring)
                                           ├ rules engine: eligible offers (menu) ─▶ Claude offer agent
                                           ├ rules engine: validate AI choice ✓/✗ → retry / fallback tiers
                                           ├ delivery + holdout split + audit log
                                           └ measurement (lift, ROI, Qini) → dashboard (SSE live)
```

Input contract — event JSON (`POST /api/events`):
`{"event_id","customer_id","type","ts"(ISO, +05:30),"product_id","price_inr","session_id","device"}`
Types: `session_start view search add_to_cart remove_from_cart checkout_start order return points_earned points_redeemed coupon_redeemed`.

Output contract — Decision JSON: `decision_id, customer_id, trigger, scores{churn_60d, uplift, clv_90d_inr,
uplift_group, lifecycle}, offer{type, value, min_order_inr, category, code, valid_until}, message{lang, text},
ai{model, reason, attempts, tier}, rule_checks[{rule, passed, reason}], status, channel, holdout`.

## Repository layout

```
backend/app/      main.py config.py db.py
  models/         SQLModel tables
  data/           ingest_*.py calibrate.py generate.py simulator.py realism.py
  analytics/      features.py churn.py uplift.py clv.py segments.py
  rules/          schema.py engine.py budget.py message_checks.py default_policy.yaml
  agent/          client.py offer_agent.py rule_author.py analyst.py prompts/
  bot/            telegram_bot.py onboarding.py cards.py
  experiments/    policy_sim.py holdout.py stats.py
  api/            FastAPI routers
backend/tests/    pytest + hypothesis; Claude is always mocked in unit tests
frontend/         Vite + React + TS + Tailwind + shadcn/ui + Recharts + TanStack Query
calibration/      committed derived stats (*.json) + india.yaml
data/raw/         git-ignored downloads
scripts/          bootstrap_data.py seed_db.py
```

## Commands (Windows; run from repo root unless noted)

```bash
# backend (uv manages Python 3.12 and the venv in backend/.venv)
cd backend && uv sync                               # install deps
cd backend && uv run uvicorn app.main:app --reload  # API on http://localhost:8000 (docs at /docs)
cd backend && uv run python -m app.bot.telegram_bot # bot in polling mode (local)
cd backend && uv run pytest                         # tests
cd backend && uv run ruff check . && uv run ruff format .

# frontend
cd frontend && npm install && npm run dev           # dashboard on http://localhost:5173
```

## Conventions

- Python 3.12, full type hints, Pydantic v2 models for every boundary (API, policy, Claude I/O), ruff for lint/format.
- Money is integer **₹ (INR)**, formatted `en-IN` (₹1,49,999). Time is **IST (Asia/Kolkata)**: the DB stores
  naive IST wall-clock datetimes; the API attaches `+05:30` at the boundary.
- Rules: the policy is `backend/app/rules/default_policy.yaml` (validated by `schema.py`, unknown keys rejected).
  `engine.validate()` returns every `Check(rule, passed, reason)`; `engine.menu()` is the only list of offers
  Claude may choose from. Offer choice is **Option A** (user's decision): pooled uplift model for *who*, measured
  campaign effects for *which* (`rules/effects.py`); per-person models only where they beat random on held-out
  data. New rules need a unit test in `tests/test_rules.py` and an invariant in `tests/test_property.py`.
- Agent: `app/agent/pipeline.decide()` is the only entry point for AI decisions. Claude sees only `engine.menu()`;
  every proposal goes through `engine.validate()` (with its message) before acceptance and once more before
  return. Sonnet 5.5 at effort `low`, strict tools, `tool_choice` auto (forced tool use 400s), `fallbacks: "default"`
  on Sonnet only. Customer text/names go through `agent/guard.py` and inside `<customer_data>` tags. Unit tests use
  `tests/agent_fakes.FakeLLM`, never the real API; live checks are `scripts/decide.py --ai`.
- Bot: handlers never touch the DB or Claude on the event loop; they call `app/bot/store` /
  `service.offer_for` via `asyncio.to_thread`. Every offer goes through `service.offer_for` → `store.persist` (L5
  send-time re-check + exactly-once by idempotency key). Bot tables use `NaiveDatetime` (naive IST). Only ONE
  polling bot process per token.
- Models: `scripts/train_models.py` → `customer_scores` + `model_runs` tables and `calibration/model_report.json`.
  Features are point-in-time (`build_features(tables, as_of)` only sees rows before `as_of`). Uplift is learned
  from the platform's past randomised campaigns (`campaign_assignments`), never from hidden traits; the
  ground-truth comparison lives in the script, outside `app/`. Uplift models are kept small on purpose.
- Data pipeline: `scripts/bootstrap_data.py` (sampled downloads → `calibration/`), then `scripts/seed_db.py`
  (population + 12-month history → DB + `calibration/realism_report.json`). Market assumptions that real data
  doesn't cover live in `calibration/india.yaml` — edit there, never hard-code them.
- Rules are pure functions with no I/O; every rule returns a named `Check(rule, passed, reason)`.
- Claude models: `claude-sonnet-5-5` (offer agent, bot chat), `claude-haiku-5-5` (copy, critic, T3 fallback,
  batch enrichment), `claude-opus-5-5` (plain-English rule authoring, analyst). Use prompt caching for the policy and
  catalogue context. 8 s timeout, ≤ 6 tool calls per decision, circuit breaker, daily token budget.
  Consult the `claude-api` skill before writing Anthropic SDK code.
- Tests: rules get unit + property tests; agent code is tested with a scripted fake Claude (bad offers, wrong ₹ in
  text, invalid JSON, timeouts, 429/500) asserting the correct guardrail layer and fallback tier fire.
- Small commits, imperative messages (`Add rules engine frequency cap`).

## Documentation (always keep current)

`docs/PROJECT_LOG.md` is the project's record; the user builds the final submission PDF from it. Whenever a step,
feature, dataset, model result, decision, or notable fix lands, update it **in the same commit** as the work:
- what was built and where (file links), with the real numbers from the run (counts, metrics, ₹, pass/fail);
- where data or parameters came from (source, procedure, seed) and any assumption added to `india.yaml`;
- decisions with who made them (user or Claude) and why; problems hit and how they were fixed;
- a timeline line (IST time, which session) and the "Last updated" stamp; keep §11 headline numbers current.
Never put secrets, tokens, or `.env` values in it.

## Two sessions in this folder

The user runs a **main build session** and a **docs session** side by side. Context is shared through files
(this file, `docs/PROJECT_LOG.md`, `calibration/*_report.json`, `git log`), never through chat.
- **Build session** owns `backend/`, `frontend/`, `scripts/`, `calibration/`, and is the only one that runs
  `seed_db.py` / `train_models.py` (they overwrite `offeriq.db` and the reports). It adds facts for each step to
  `docs/PROJECT_LOG.md` in the same commit.
- **Docs session** reads everything but writes only in `docs/` (PDFs and log wording). It never edits code or data
  and never re-seeds or retrains.
- Both stage **only their own files by name** (`git add docs/PROJECT_LOG.md`), never `git add -A` or `git add .`,
  and never commit the other session's uncommitted work. Neither pushes; the user does.
- `docs/transcripts/` (git-ignored) holds readable exports of the main chat and the approved plan, so the docs
  session can cross-check the log against what was actually said. Facts still belong in `docs/PROJECT_LOG.md`.

## Progress

- [x] Plan approved (full plan: section-by-section in the planning session)
- [x] 0  Repo skeleton, CLAUDE.md, README, bot replies to /start (@Dilnazecom_bot)
- [x] 1  Data ingest + calibration + synthetic generator + realism report
- [x] 2  Features, churn, uplift (validated on Hillstrom), CLV, segments (`scripts/train_models.py`)
- [x] 3  Policy schema, rules engine, message checks, property tests (`backend/app/rules/`)
- [x] 3b Rules-only next-best-offer (T4), segment fallbacks (T5), templates — works with no AI (`scripts/decide.py`)
- [x] 4  Claude offer agent + harness (L0–L4 built; L5 with the bot, L6 with Health) + T1–T3 + audit log + red-team tests (`backend/app/agent/`)
- [ ] 4b Health: independent re-checker, canary customers, golden evals
- [x] 5  Full Telegram flow (quiz → digital twin → browse → offer card → feedback, holdout) (`backend/app/bot/`)
- [x] 6  React dashboard + API (login; 15 s polling instead of SSE) (`backend/app/api/`, `frontend/src/`)
- [ ] 7  NL rule editing + dry run ✓, policy simulation (Policy Lab) ✓, versioned policies ✓; campaigns, AI analyst,
       two-way bot chat open (`agent/rule_author.py`, `experiments/policy_lab.py`, `rules/policy_store.py`)
- [x] 8  Deploy (Render + Neon, one container), README with demo script; Live engine page (`api/engine.py`)
