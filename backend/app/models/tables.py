"""Database tables for the platform's data (what an e-commerce site would send OfferIQ)."""

from datetime import date, datetime

from sqlmodel import Field, SQLModel


class Customer(SQLModel, table=True):
    __tablename__ = "customers"

    customer_id: str = Field(primary_key=True)
    name: str
    age_band: str
    gender: str  # fairness auditing only — never a targeting input
    city: str
    state: str
    city_tier: str
    signup_date: date
    # Summary of order history older than the 12-month event window (a platform keeps lifetime stats).
    orders_before_window: int = 0
    last_order_before_window: date | None = None
    acquisition_channel: str
    device: str
    language_pref: str
    prime_member: bool
    prime_since: date | None = None
    preferred_payment: str
    points_balance: int = 0
    points_expiry_date: date | None = None
    opted_out: bool = False
    telegram_chat_id: int | None = Field(default=None, index=True)
    twin_of: str | None = None


class Product(SQLModel, table=True):
    __tablename__ = "products"

    product_id: str = Field(primary_key=True)
    title: str
    brand: str
    category: str = Field(index=True)
    subcategory: str | None = None
    image_url: str | None = None
    mrp_inr: int
    price_inr: int
    margin_pct: float
    rating: float | None = None
    rating_count: int = 0


class Event(SQLModel, table=True):
    __tablename__ = "events"

    event_id: str = Field(primary_key=True)
    customer_id: str = Field(index=True, foreign_key="customers.customer_id")
    type: str = Field(index=True)
    ts: datetime = Field(index=True)
    product_id: str | None = None
    price_inr: int | None = None
    session_id: str | None = None
    device: str | None = None
    order_id: str | None = None
    points: int | None = None
    query: str | None = None


class Order(SQLModel, table=True):
    __tablename__ = "orders"

    order_id: str = Field(primary_key=True)
    customer_id: str = Field(index=True, foreign_key="customers.customer_id")
    ts: datetime = Field(index=True)
    gross_inr: int
    discount_inr: int = 0
    coupon_code: str | None = None
    payment_mode: str
    status: str  # delivered | returned | rto
    return_reason: str | None = None
    points_earned: int = 0
    points_redeemed: int = 0


class OrderItem(SQLModel, table=True):
    __tablename__ = "order_items"

    id: int | None = Field(default=None, primary_key=True)
    order_id: str = Field(index=True, foreign_key="orders.order_id")
    product_id: str
    category: str
    qty: int = 1
    price_inr: int


class CampaignAssignment(SQLModel, table=True):
    """Past randomised campaigns (a platform's A/B history). Control rows have no offer."""

    __tablename__ = "campaign_assignments"

    id: int | None = Field(default=None, primary_key=True)
    campaign_id: str = Field(index=True)
    customer_id: str = Field(index=True, foreign_key="customers.customer_id")
    arm: str
    offer_type: str | None = None
    offer_value: float | None = None
    sent_at: datetime
    valid_until: datetime


class OfferDecision(SQLModel, table=True):
    """Audit log: every decision OfferIQ makes, with every rule check, whether or not an offer was sent."""

    __tablename__ = "offer_decisions"

    decision_id: str = Field(primary_key=True)
    customer_id: str = Field(index=True, foreign_key="customers.customer_id")
    created_at: datetime = Field(index=True)
    trigger: str
    tier: str  # T1..T6
    status: str = Field(index=True)  # sent | holdout | queued | suppressed | claimed | dismissed
    offer_id: str | None = None
    offer_type: str | None = None
    offer_value: float | None = None
    category: str | None = None
    code: str | None = None
    valid_until: datetime | None = None
    message_lang: str | None = None
    message_text: str | None = None
    expected_cost_inr: float | None = None
    expected_profit_inr: float | None = None
    uplift: float | None = None
    policy_version: int
    rule_checks: str  # JSON list of {rule, passed, reason}
    explanation: str  # JSON: why this offer (or none), shown in the dashboard and "Why me?"
    ai: str | None = None  # JSON: model, attempts, reasons (step 4)


class SimHiddenTrait(SQLModel, table=True):
    """Simulator ground truth. Analytics and models must NEVER read this table."""

    __tablename__ = "sim_hidden_traits"

    customer_id: str = Field(primary_key=True, foreign_key="customers.customer_id")
    purchase_rate_per_day: float
    dropout_prob_per_order: float
    alive: bool
    price_sensitivity: float
    offer_responsiveness: float  # relative lift in purchase rate from a plain nudge (Hillstrom-calibrated)
    sleeping_dog: bool
    category_affinity: str  # JSON {category: weight}
