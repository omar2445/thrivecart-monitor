"""
Background scheduler: checks every hour for subscriptions whose next_payment_date
is more than OVERDUE_HOURS hours in the past without a successful payment.
Sends one email alert per billing cycle (no duplicate spam).
"""

import asyncio
import logging
import os
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.orm import Session

from database import SessionLocal
from email_service import send_overdue_alert
from models import OverdueNotification, Subscription

logger = logging.getLogger(__name__)

OVERDUE_HOURS = int(os.getenv("OVERDUE_HOURS", "24"))


def _find_overdue(db: Session) -> list[Subscription]:
    """Return subscriptions that need a payment alert:
    - status='failed': ThriveCart explicitly reported a payment failure
    - status='active' but next_payment_date is 24h+ past: missed payment, no webhook received
    """
    cutoff = datetime.utcnow() - timedelta(hours=OVERDUE_HOURS)
    from sqlalchemy import or_
    return (
        db.query(Subscription)
        .filter(
            Subscription.next_payment_date.isnot(None),
            or_(
                Subscription.status == "failed",
                (Subscription.status == "active") & (Subscription.next_payment_date <= cutoff),
            )
        )
        .all()
    )


def _already_notified(db: Session, sub: Subscription) -> bool:
    """True if we already sent an alert for this exact billing cycle."""
    existing = (
        db.query(OverdueNotification)
        .filter_by(thrivecart_subscription_id=sub.thrivecart_subscription_id)
        .filter(OverdueNotification.billing_cycle_date == sub.next_payment_date)
        .first()
    )
    return existing is not None


def _record_notification(db: Session, sub: Subscription, notify_email: str):
    note = OverdueNotification(
        thrivecart_subscription_id=sub.thrivecart_subscription_id,
        billing_cycle_date=sub.next_payment_date,
        sent_to=notify_email,
    )
    db.add(note)
    db.commit()


async def check_overdue_payments():
    notify_email = os.getenv("NOTIFY_EMAIL", "")
    logger.info("Running overdue payment check (threshold: %dh)", OVERDUE_HOURS)

    db: Session = SessionLocal()
    try:
        overdue_subs = _find_overdue(db)
        if not overdue_subs:
            logger.info("No overdue subscriptions found.")
            return

        # Filter out those already notified for this billing cycle
        to_notify = [s for s in overdue_subs if not _already_notified(db, s)]
        if not to_notify:
            logger.info("All overdue subscriptions already notified.")
            return

        logger.info("Found %d subscription(s) to alert about.", len(to_notify))

        payload = [
            {
                "customer_name": s.customer_name,
                "customer_email": s.customer_email,
                "product_name": s.product_name,
                "amount": s.amount or 0.0,
                "next_payment_date": s.next_payment_date,
            }
            for s in to_notify
        ]

        await send_overdue_alert(payload)
        logger.info("Overdue alert email sent to %s", notify_email)

        for sub in to_notify:
            _record_notification(db, sub, notify_email)

    except Exception as exc:
        logger.exception("Error during overdue check: %s", exc)
    finally:
        db.close()


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    # Run immediately on startup, then every hour
    scheduler.add_job(check_overdue_payments, "interval", hours=1, id="overdue_check",
                      next_run_time=datetime.utcnow())
    return scheduler
