"""
ThriveCart webhook event processor.

ThriveCart sends these relevant events:
  order_product           — new subscription created (first payment)
  subscription_rebill     — recurring payment succeeded
  subscription_payment_failed — recurring payment failed
  subscription_cancelled  — subscription cancelled by customer/admin
  subscription_expired    — subscription ended after all rebills

Webhook payload shape (simplified):
{
  "thrivecart": {
    "event":    "subscription_rebill",
    "customer": { "email": "...", "name": "...", ... },
    "product":  { "id": 123, "name": "...", ... },
    "order": {
      "id": "ORD123",
      "subscription_id": "SUB123",
      "order_total": "99.00",
      "next_rebill_date": 1720000000   ← Unix timestamp
    }
  }
}
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from models import Subscription, PaymentEvent

logger = logging.getLogger(__name__)


def _parse_next_date(order: dict) -> datetime | None:
    """Extract next billing date from order dict (ThriveCart sends Unix timestamp)."""
    raw = order.get("next_rebill_date") or order.get("rebill_date")
    if not raw:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _upsert_subscription(db: Session, payload: dict, status: str) -> Subscription:
    tc = payload.get("thrivecart", payload)  # handle both wrapped and flat payloads
    customer = tc.get("customer", {})
    product = tc.get("product", {})
    order = tc.get("order", {})

    sub_id = str(order.get("subscription_id") or order.get("id") or "unknown")
    email = customer.get("email", "").strip().lower()
    name = f"{customer.get('firstname', '')} {customer.get('lastname', '')}".strip()
    product_name = product.get("name", "")
    product_id = str(product.get("id", ""))
    amount = float(order.get("order_total") or order.get("amount") or 0)
    next_payment = _parse_next_date(order)
    now = datetime.utcnow()

    sub = db.query(Subscription).filter_by(thrivecart_subscription_id=sub_id).first()
    if sub is None:
        sub = Subscription(
            thrivecart_subscription_id=sub_id,
            customer_email=email,
            customer_name=name,
            product_name=product_name,
            product_id=product_id,
            amount=amount,
            status=status,
            last_payment_date=now if status == "active" else None,
            next_payment_date=next_payment,
            created_at=now,
            updated_at=now,
        )
        db.add(sub)
    else:
        sub.status = status
        sub.customer_email = email or sub.customer_email
        sub.customer_name = name or sub.customer_name
        sub.product_name = product_name or sub.product_name
        sub.amount = amount or sub.amount
        if status == "active":
            sub.last_payment_date = now
        if next_payment:
            sub.next_payment_date = next_payment
        sub.updated_at = now

    db.commit()
    db.refresh(sub)
    return sub


def _record_event(db: Session, sub_id: str, event_type: str, amount: float, payload: dict):
    event = PaymentEvent(
        thrivecart_subscription_id=sub_id,
        event_type=event_type,
        amount=amount,
        raw_payload=json.dumps(payload),
    )
    db.add(event)
    db.commit()


def process_webhook(payload: Any, db: Session) -> dict:
    """Main entry point. Returns a status dict for logging."""
    tc = payload.get("thrivecart", payload) if isinstance(payload, dict) else {}
    event = (tc.get("event") or "").lower().strip()
    order = tc.get("order", {})
    sub_id = str(order.get("subscription_id") or order.get("id") or "unknown")
    amount = float(order.get("order_total") or order.get("amount") or 0)

    logger.info("Received ThriveCart event: %s  subscription_id: %s", event, sub_id)

    if event in ("order_product", "subscription_rebill", "order.success",
                 "subscription.payment.success", "subscription_payment_success"):
        sub = _upsert_subscription(db, payload, status="active")
        _record_event(db, sub_id, event, amount, payload)
        return {"handled": True, "event": event, "subscription_id": sub_id, "status": "active"}

    elif event in ("subscription_payment_failed", "subscription.payment.failed",
                   "order.failed", "subscription_failed"):
        sub = _upsert_subscription(db, payload, status="failed")
        _record_event(db, sub_id, event, 0, payload)
        return {"handled": True, "event": event, "subscription_id": sub_id, "status": "failed"}

    elif event in ("subscription_cancelled", "subscription.cancelled",
                   "order.cancelled", "subscription_canceled"):
        sub = _upsert_subscription(db, payload, status="cancelled")
        _record_event(db, sub_id, event, 0, payload)
        return {"handled": True, "event": event, "subscription_id": sub_id, "status": "cancelled"}

    elif event in ("subscription_expired", "subscription.expired"):
        sub = _upsert_subscription(db, payload, status="expired")
        _record_event(db, sub_id, event, 0, payload)
        return {"handled": True, "event": event, "subscription_id": sub_id, "status": "expired"}

    else:
        logger.warning("Unhandled ThriveCart event type: %s", event)
        _record_event(db, sub_id, event, 0, payload)
        return {"handled": False, "event": event, "subscription_id": sub_id}
