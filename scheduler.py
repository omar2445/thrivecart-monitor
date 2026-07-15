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
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from database import SessionLocal
from email_service import send_overdue_alert, send_unpaid_report
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


def _find_unpaid(db: Session, since: datetime | None = None, until: datetime | None = None) -> list[dict]:
    """Recurring subscriptions whose payment date is past without renewal.
    Optional since/until restrict to due dates within that window."""
    now = datetime.utcnow()
    # Only report missed payments from the last N days — older is stale history
    stale_cutoff = now - timedelta(days=int(os.getenv("UNPAID_MAX_DAYS", "120")))
    from sqlalchemy import or_
    query = (
        db.query(Subscription)
        .filter(
            or_(
                Subscription.subscription_type == "recurring",
                Subscription.subscription_type.is_(None),
            ),
            Subscription.next_payment_date.isnot(None),
            Subscription.next_payment_date >= stale_cutoff,
            Subscription.next_payment_date <= now,
            or_(
                Subscription.status == "failed",
                Subscription.status == "active",
                # Cancelled after declines: still owes the missed payment
                Subscription.status == "cancelled",
            ),
        )
    )
    if since is not None:
        query = query.filter(Subscription.next_payment_date >= since)
    if until is not None:
        query = query.filter(Subscription.next_payment_date < until)
    subs = query.order_by(Subscription.next_payment_date).all()
    return [
        {
            "customer_name": s.customer_name,
            "customer_email": s.customer_email,
            "product_name": s.product_name,
            "amount": s.amount or 0.0,
            "next_payment_date": s.next_payment_date,
        }
        for s in subs
    ]


MONTHS_FR = ["janvier", "février", "mars", "avril", "mai", "juin",
             "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


async def send_report(period_label: str, since: datetime | None = None, until: datetime | None = None):
    """Build and email the unpaid report for the given due-date window."""
    logger.info("Sending %s unpaid report...", period_label)
    db: Session = SessionLocal()
    try:
        unpaid = _find_unpaid(db, since=since, until=until)
        await send_unpaid_report(unpaid, period_label)
        logger.info("%s report sent: %d unpaid, total %.2f $",
                    period_label, len(unpaid), sum(u["amount"] for u in unpaid))
    except Exception as exc:
        logger.exception("Error sending %s report: %s", period_label, exc)
    finally:
        db.close()


async def send_weekly_report():
    """Clients whose payment was due in the last 7 days and who didn't pay."""
    now = datetime.utcnow()
    since = now - timedelta(days=7)
    label = f"hebdomadaire (du {since.strftime('%d/%m')} au {now.strftime('%d/%m/%Y')})"
    await send_report(label, since=since, until=now)


async def send_monthly_report():
    """Clients whose payment was due during the previous calendar month and who didn't pay."""
    now = datetime.utcnow()
    first_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = first_this_month
    last_month_start = (first_this_month - timedelta(days=1)).replace(day=1)
    label = f"mensuel — {MONTHS_FR[last_month_start.month - 1]} {last_month_start.year}"
    await send_report(label, since=last_month_start, until=last_month_end)


def create_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Weekly report: every Monday at 09:00 UTC
    scheduler.add_job(send_weekly_report, CronTrigger(day_of_week="mon", hour=9, minute=0),
                      id="weekly_report")
    # Monthly report: 1st of each month at 09:00 UTC
    scheduler.add_job(send_monthly_report, CronTrigger(day=1, hour=9, minute=0),
                      id="monthly_report")
    # NOTE: the hourly 24h-overdue alert is disabled for now — it will become
    # the client-facing reminder email (to be implemented later).
    # scheduler.add_job(check_overdue_payments, "interval", hours=1, id="overdue_check")
    return scheduler
