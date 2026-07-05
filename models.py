from sqlalchemy import Column, Integer, String, DateTime, Float, Text
from datetime import datetime, timezone
from database import Base


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    thrivecart_subscription_id = Column(String, unique=True, index=True)
    customer_email = Column(String, index=True, nullable=False)
    customer_name = Column(String, default="")
    product_name = Column(String, default="")
    product_id = Column(String, default="")
    amount = Column(Float, nullable=True)
    # active | failed | cancelled | expired
    status = Column(String, default="active")
    # recurring | one_time
    subscription_type = Column(String, default="recurring")
    last_payment_date = Column(DateTime, nullable=True)
    next_payment_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=_utcnow)
    updated_at = Column(DateTime, default=_utcnow, onupdate=_utcnow)


class PaymentEvent(Base):
    __tablename__ = "payment_events"

    id = Column(Integer, primary_key=True, index=True)
    thrivecart_subscription_id = Column(String, index=True)
    event_type = Column(String)
    amount = Column(Float, nullable=True)
    event_date = Column(DateTime, default=_utcnow)
    raw_payload = Column(Text)


class OverdueNotification(Base):
    __tablename__ = "overdue_notifications"

    id = Column(Integer, primary_key=True, index=True)
    thrivecart_subscription_id = Column(String, index=True)
    # The billing date this notification covers (prevents duplicate alerts per cycle)
    billing_cycle_date = Column(DateTime, nullable=False)
    sent_at = Column(DateTime, default=_utcnow)
    sent_to = Column(String)
