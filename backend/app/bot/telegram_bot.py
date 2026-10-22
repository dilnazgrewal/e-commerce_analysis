"""OfferIQ Telegram bot: classmates play customers.

/start → consent → 5-tap quiz → matched to a synthetic "digital twin" (their history becomes yours) → a first
offer decided by the AI pipeline. /browse shows real catalogue products; "Add to cart" without checking out
triggers an abandoned-cart decision 90 s later. Offer cards have Claim / Not for me / Why me?. Every decision
(sent or not) is logged with its rule checks; 10% of chats are a live holdout group that never gets offers.

Run locally in polling mode:  uv run python -m app.bot.telegram_bot
"""

import asyncio
import json
import logging
import os
from datetime import timedelta

from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.agent.harness import Harness
from app.bot import cards, store, twin
from app.bot.service import Services, ist_now, next_morning, offer_for
from app.config import get_settings
from app.db import get_engine
from app.models import BotUser
from app.rules.effects import OfferEffects
from app.rules.schema import load_policy
from app.rules.templates import CATEGORY_LABELS

log = logging.getLogger("offeriq.bot")

CART_TRIGGER_DELAY_S = 90
HTML = ParseMode.HTML

WELCOME = (
    "🙏 <b>Namaste, {name}! Welcome to OfferIQ.</b>\n\n"
    "OfferIQ is a hackathon project: an engine that decides which shopping offer suits you, and checks every "
    "offer against the shop's rules before you see it.\n\n"
    "🔒 Everything here is <b>simulated</b>: no real orders, no payments. We store only your first name and "
    "your taps.\n\n"
    "Take a 5-tap quiz, meet your shopping twin and get your first offer?"
)

HELP = (
    "<b>OfferIQ commands</b>\n"
    "/start: quiz and meet your shopping twin\n"
    "/browse: look at real products (simulated cart)\n"
    "/offer: ask OfferIQ for an offer now\n"
    "/history: your recent offers\n"
    "/stop: stop all offers · /resume: start again\n"
    "/help: this message"
)


def svc(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.bot_data["svc"]


async def reply(update: Update, text: str, **kwargs) -> None:
    await update.effective_message.reply_text(text, parse_mode=HTML, **kwargs)


# --------------------------------------------------------------------------- onboarding


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = user.first_name if user and user.first_name else "there"
    chat_id = update.effective_chat.id
    log.info("start from chat_id=%s", chat_id)
    s = svc(context)
    existing = await asyncio.to_thread(store.get_user, s.engine, chat_id)
    if existing is None:
        bot_user = BotUser(
            chat_id=chat_id, first_name=name[:40], created_at=ist_now(), holdout=twin.chat_in_holdout(chat_id)
        )
        await asyncio.to_thread(store.save_user, s.engine, bot_user)
    await reply(
        update, WELCOME.format(name=name), reply_markup=cards.keyboard([[("consent", "✅ Let's go")]])
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, HELP)


async def on_consent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    s = svc(context)
    user = await asyncio.to_thread(store.get_user, s.engine, update.effective_chat.id)
    if user is None:
        await reply(update, "Please send /start first.")
        return
    user.consent_at, user.quiz = ist_now(), "{}"
    await asyncio.to_thread(store.save_user, s.engine, user)
    text, markup = cards.quiz_question(0)
    await query.edit_message_reply_markup(None)
    await reply(update, text, reply_markup=markup)


async def on_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, field, value = query.data.split(":", 2)
    s = svc(context)
    user = await asyncio.to_thread(store.get_user, s.engine, update.effective_chat.id)
    if user is None or user.consent_at is None:
        await reply(update, "Please send /start first.")
        return
    answers = json.loads(user.quiz or "{}") | {field: value}
    user.quiz = json.dumps(answers)
    if field == "language":
        user.language = value
    await asyncio.to_thread(store.save_user, s.engine, user)
    await query.edit_message_reply_markup(None)
    step = [f for f, _, _ in cards.QUIZ].index(field) + 1
    if step < len(cards.QUIZ):
        text, markup = cards.quiz_question(step)
        await reply(update, text, reply_markup=markup)
        return
    await reply(update, "🔎 Finding your shopping twin among 5,000 customers…")
    customer_id = await asyncio.to_thread(twin.match, s.engine, answers, update.effective_chat.id)
    await asyncio.to_thread(store.bind_twin, s.engine, user.chat_id, customer_id, user.language)
    user.twin_customer_id = customer_id
    await asyncio.to_thread(store.save_user, s.engine, user)
    summary = await asyncio.to_thread(store.twin_summary, s.engine, customer_id, ist_now())
    await reply(update, cards.twin_text(user.first_name, summary))
    await reply(update, "⏳ Deciding your first offer (an AI picks it, the shop's rules check it)…")
    await send_offer(context, user, "welcome", f"welcome:{user.chat_id}:{customer_id}", explain_empty=True)
    await reply(update, "Try /browse to look at products, or /help for everything else.")


# --------------------------------------------------------------------------- offers


async def send_offer(
    context: ContextTypes.DEFAULT_TYPE,
    user: BotUser,
    trigger: str,
    key: str | None,
    customer_text: str | None = None,
    explain_empty: bool = False,
) -> None:
    s = svc(context)
    now = ist_now()
    try:
        decision = await asyncio.to_thread(offer_for, s, user, trigger, now, customer_text, key)
    except Exception:  # noqa: BLE001 - the bot must keep running; the error is logged
        log.exception("decision failed for chat %s", user.chat_id)
        await context.bot.send_message(user.chat_id, "Sorry, something went wrong deciding your offer.")
        return
    log.info(
        "decision %s chat=%s trigger=%s tier=%s status=%s",
        decision.decision_id,
        user.chat_id,
        trigger,
        decision.tier,
        decision.status,
    )
    d = decision.to_dict()
    if decision.status == "sent":
        photo = await asyncio.to_thread(offer_photo, s, user, d)
        text, markup = cards.offer_text(d), cards.offer_buttons(decision.decision_id)
        try:
            if photo:
                await context.bot.send_photo(
                    user.chat_id, photo, caption=text, parse_mode=HTML, reply_markup=markup
                )
                return
        except Exception:  # noqa: BLE001 - fall back to text if Telegram can't fetch the image
            log.warning("photo failed for %s", photo)
        await context.bot.send_message(user.chat_id, text, parse_mode=HTML, reply_markup=markup)
    elif decision.status == "queued":
        when = next_morning(now, s.policy)
        context.job_queue.run_once(
            queued_job,
            when=(when - now).total_seconds(),
            data={"chat_id": user.chat_id, "trigger": trigger},
            name=f"queued:{user.chat_id}",
        )
        await context.bot.send_message(
            user.chat_id,
            f"🌙 It's quiet hours (no offers 9 pm–9 am). Your offer will "
            f"arrive after {when:%H:%M} on {when:%d %b}.",
        )
    elif explain_empty and decision.status in ("suppressed", "holdout"):
        why = decision.explanation.get("why", "")
        prefix = "🧪 " if decision.status == "holdout" else "⏸ No offer for you right now. "
        await context.bot.send_message(user.chat_id, prefix + why)


def offer_photo(s: Services, user: BotUser, d: dict) -> str | None:
    item = store.last_cart_item(s.engine, user.twin_customer_id, ist_now() - timedelta(days=7))
    if item and item.get("image_url"):
        return item["image_url"]
    category = d["offer"].get("category")
    if not category:
        answers = json.loads(user.quiz or "{}")
        group = twin.CATEGORY_GROUPS.get(answers.get("category"), ["Home_and_Kitchen"])
        category = group[0]
    items = store.products(s.engine, [category], None, 0, 1)
    return items[0]["image_url"] if items else None


async def queued_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    user = await asyncio.to_thread(store.get_user, svc(context).engine, data["chat_id"])
    if user and user.twin_customer_id and not user.opted_out:
        await send_offer(context, user, f"{data['trigger']}:after_quiet_hours", None, explain_empty=True)


async def cart_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = context.job.data
    s = svc(context)
    user = await asyncio.to_thread(store.get_user, s.engine, data["chat_id"])
    if not user or user.opted_out or not user.twin_customer_id:
        return
    item = await asyncio.to_thread(
        store.last_cart_item, s.engine, user.twin_customer_id, ist_now() - timedelta(minutes=30)
    )
    if item:  # still in the cart, not ordered: the abandoned-cart trigger fires
        await send_offer(context, user, "cart_abandoned", f"cart:{user.chat_id}:{data['added_at']}")


async def offer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ready_user(update, context)
    if user:
        await reply(update, "⏳ Asking OfferIQ…")
        await send_offer(
            context, user, "requested", f"req:{user.chat_id}:{ist_now():%Y%m%d%H%M}", explain_empty=True
        )


async def ready_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> BotUser | None:
    user = await asyncio.to_thread(store.get_user, svc(context).engine, update.effective_chat.id)
    if user is None or not user.twin_customer_id:
        await reply(update, "First take the quick quiz: send /start.")
        return None
    if user.opted_out:
        await reply(update, "You've stopped offers. Send /resume to start again.")
        return None
    return user


async def on_offer_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, decision_id = query.data.split(":", 1)
    s = svc(context)
    row = await asyncio.to_thread(store.get_decision, s.engine, decision_id)
    user = await asyncio.to_thread(store.get_user, s.engine, update.effective_chat.id)
    if row is None or user is None or row["customer_id"] != user.twin_customer_id:
        await query.answer("This offer isn't yours or no longer exists.", show_alert=True)
        return
    now = ist_now()
    if action == "why":
        await query.answer()
        await asyncio.to_thread(store.add_feedback, s.engine, decision_id, user.chat_id, "why", None, now)
        await reply(update, cards.why_text(row))
        return
    if row["status"] not in ("sent",):
        await query.answer(f"This offer is already {row['status']}.", show_alert=True)
        return
    if action == "claim":
        if str(row["valid_until"]) < now.isoformat(sep=" "):
            await query.answer("Sorry, this offer has expired.", show_alert=True)
            return
        await query.answer("Claimed!")
        item = await asyncio.to_thread(
            store.last_cart_item, s.engine, user.twin_customer_id, now - timedelta(days=7)
        )
        order = await asyncio.to_thread(store.record_claim_order, s.engine, row, item, now)
        await asyncio.to_thread(store.set_status, s.engine, decision_id, "claimed")
        await asyncio.to_thread(
            store.add_feedback, s.engine, decision_id, user.chat_id, "claimed", order["order_id"], now
        )
        what = f" on {item['title'][:60]}" if item else ""
        saved = f" You saved {cards.inr(order['discount_inr'])}." if order["discount_inr"] else ""
        await query.edit_message_reply_markup(cards.keyboard([[(f"why:{decision_id}", "❓ Why me?")]]))
        await reply(
            update,
            f"✅ <b>Claimed!</b> Simulated order {order['order_id']}{cards.escape(what)} for "
            f"{cards.inr(order['price_inr'])}.{saved} Thanks for helping test OfferIQ 🙏",
        )
    elif action == "nope":
        await query.answer("Got it.")
        await asyncio.to_thread(store.set_status, s.engine, decision_id, "dismissed")
        await asyncio.to_thread(
            store.add_feedback, s.engine, decision_id, user.chat_id, "dismissed", None, now
        )
        await query.edit_message_reply_markup(cards.keyboard([[(f"why:{decision_id}", "❓ Why me?")]]))
        await reply(
            update, "No problem. What was off? (optional)", reply_markup=cards.reason_buttons(decision_id)
        )


async def on_reason(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Thanks!")
    _, decision_id, reason = query.data.split(":", 2)
    await asyncio.to_thread(
        store.add_feedback,
        svc(context).engine,
        decision_id,
        update.effective_chat.id,
        "reason",
        reason,
        ist_now(),
    )
    await query.edit_message_text("Thanks, that helps OfferIQ learn. 🙏")


# --------------------------------------------------------------------------- browsing


async def browse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await ready_user(update, context):
        rows = [[(f"b:{key}:0", label)] for key, label in cards.BROWSE]
        await reply(update, "What would you like to look at?", reply_markup=cards.keyboard(rows))


async def on_browse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, group, offset = query.data.split(":")
    s = svc(context)
    user = await ready_user(update, context)
    if not user:
        return
    budget = json.loads(user.quiz or "{}").get("budget")
    items = await asyncio.to_thread(
        store.products, s.engine, twin.CATEGORY_GROUPS[group], cards.BUDGET_CAP.get(budget), int(offset)
    )
    now = ist_now()
    session = f"TGS-{user.chat_id}-{now:%Y%m%d%H}"
    events = [{"type": "session_start", "session_id": session}] if offset == "0" else []
    events += [
        {"type": "view", "session_id": session, "product_id": p["product_id"], "price_inr": p["price_inr"]}
        for p in items
    ]
    await asyncio.to_thread(store.add_events, s.engine, user.twin_customer_id, events, now)
    for p in items:
        try:
            await context.bot.send_photo(
                user.chat_id,
                p["image_url"],
                caption=cards.product_caption(p),
                parse_mode=HTML,
                reply_markup=cards.product_buttons(p["product_id"]),
            )
        except Exception:  # noqa: BLE001
            await context.bot.send_message(
                user.chat_id,
                cards.product_caption(p),
                parse_mode=HTML,
                reply_markup=cards.product_buttons(p["product_id"]),
            )
    label = CATEGORY_LABELS.get(twin.CATEGORY_GROUPS[group][0], group)
    more = cards.keyboard([[(f"b:{group}:{int(offset) + 3}", f"More {label} ▶")]])
    await context.bot.send_message(user.chat_id, "Tap ❤️ to add to your (simulated) cart.", reply_markup=more)


async def on_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    product_id = query.data.split(":", 1)[1]
    s = svc(context)
    user = await ready_user(update, context)
    if not user:
        await query.answer()
        return
    item = await asyncio.to_thread(store.product, s.engine, product_id)
    now = ist_now()
    await asyncio.to_thread(
        store.add_events,
        s.engine,
        user.twin_customer_id,
        [
            {
                "type": "add_to_cart",
                "session_id": f"TGS-{user.chat_id}-{now:%Y%m%d%H}",
                "product_id": product_id,
                "price_inr": item["price_inr"] if item else None,
            }
        ],
        now,
    )
    await query.answer("Added to cart 🛒")
    context.job_queue.run_once(
        cart_job,
        when=CART_TRIGGER_DELAY_S,
        data={"chat_id": user.chat_id, "added_at": f"{now:%Y%m%d%H%M%S}"},
        name=f"cart:{user.chat_id}",
    )
    await reply(
        update,
        f"🛒 Added <b>{cards.escape(str(item['title'])[:60]) if item else 'item'}</b> to your simulated "
        "cart. No checkout here, so watch what OfferIQ does if you leave it there…",
    )


# --------------------------------------------------------------------------- history, stop, resume


async def history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await asyncio.to_thread(store.get_user, svc(context).engine, update.effective_chat.id)
    if user is None or not user.twin_customer_id:
        await reply(update, "First take the quick quiz: send /start.")
        return
    rows = await asyncio.to_thread(store.recent_decisions, svc(context).engine, user.twin_customer_id)
    await reply(update, cards.history_text(rows))


async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(store.set_opt_out, svc(context).engine, update.effective_chat.id, True)
    await reply(update, "🛑 Done: you won't get any more offers. Send /resume if you change your mind.")


async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(store.set_opt_out, svc(context).engine, update.effective_chat.id, False)
    await reply(update, "▶️ Offers are back on. Try /offer or /browse.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await reply(update, "Thanks! I don't chat yet. Use the buttons, or /browse, /offer and /help.")


# --------------------------------------------------------------------------- plumbing


async def log_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log every incoming update (type + chat id only, never message content)."""
    chat_id = update.effective_chat.id if update.effective_chat else None
    kind = next((k for k, v in update.to_dict().items() if k != "update_id" and v), "unknown")
    log.info("update %s: %s from chat_id=%s", update.update_id, kind, chat_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("handler error", exc_info=context.error)


async def announce(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Quiz and meet your shopping twin"),
            BotCommand("browse", "Look at products (simulated cart)"),
            BotCommand("offer", "Ask OfferIQ for an offer now"),
            BotCommand("history", "Your recent offers"),
            BotCommand("stop", "Stop all offers"),
            BotCommand("resume", "Start offers again"),
            BotCommand("help", "All commands"),
        ]
    )
    me = await application.bot.get_me()
    log.info("Bot is live: https://t.me/%s, send it /start", me.username)


def build_application(token: str, services: Services | None = None) -> Application:
    application = Application.builder().token(token).post_init(announce).concurrent_updates(True).build()
    if services is not None:
        application.bot_data["svc"] = services
    application.add_handler(TypeHandler(Update, log_update), group=-1)
    application.add_error_handler(on_error)
    for name, handler in [
        ("start", start),
        ("help", help_command),
        ("offer", offer_command),
        ("browse", browse),
        ("history", history),
        ("stop", stop),
        ("resume", resume),
    ]:
        application.add_handler(CommandHandler(name, handler))
    application.add_handler(CallbackQueryHandler(on_consent, pattern=r"^consent$"))
    application.add_handler(CallbackQueryHandler(on_quiz, pattern=r"^q:"))
    application.add_handler(CallbackQueryHandler(on_browse, pattern=r"^b:"))
    application.add_handler(CallbackQueryHandler(on_cart, pattern=r"^cart:"))
    application.add_handler(CallbackQueryHandler(on_reason, pattern=r"^rsn:"))
    application.add_handler(CallbackQueryHandler(on_offer_button, pattern=r"^(claim|nope|why):"))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return application


def make_services() -> Services:
    engine = get_engine()
    store.ensure_schema(engine)
    policy_file = os.environ.get("OFFERIQ_POLICY_FILE")  # e.g. a policy version under test
    policy = load_policy(policy_file) if policy_file else load_policy()
    log.info("Policy: %s (version %s)", policy.name, policy.version)
    return Services(engine, policy, OfferEffects.from_report(), Harness())


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    # httpx logs full request URLs, which contain the bot token — keep it quiet.
    for noisy in ("httpx", "httpx2", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    token = get_settings().telegram_bot_token
    if token is None or not token.get_secret_value():
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Add it to the repo-root .env (see .env.example).")

    log.info("Starting OfferIQ bot in polling mode")
    build_application(token.get_secret_value(), make_services()).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
