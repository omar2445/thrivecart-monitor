import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from sqlalchemy.orm import Session

from database import get_db, init_db
from scheduler import create_scheduler
from webhook import process_webhook

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

THRIVECART_SECRET = os.getenv("THRIVECART_SECRET", "")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("Scheduler started — checking for overdue payments every hour.")
    yield
    scheduler.shutdown()
    logger.info("Scheduler stopped.")


app = FastAPI(
    title="ThriveCart Payment Monitor",
    description="Receives ThriveCart webhooks and alerts on overdue subscriptions.",
    version="1.0.0",
    lifespan=lifespan,
)


def _verify_signature(body: bytes, signature: str | None) -> bool:
    """Verify the HMAC-SHA256 signature ThriveCart attaches to webhooks."""
    if not THRIVECART_SECRET:
        logger.warning("THRIVECART_SECRET not set — skipping signature verification.")
        return True
    if not signature:
        return False
    expected = hmac.new(
        THRIVECART_SECRET.encode(), body, digestmod=hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature.removeprefix("sha256="))


@app.get("/webhook/thrivecart", tags=["Webhooks"])
async def thrivecart_webhook_verify():
    """ThriveCart pings this with GET to verify the URL is reachable."""
    return {"ok": True}


@app.post("/webhook/thrivecart", tags=["Webhooks"])
async def thrivecart_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_thrivecart_signature: str | None = Header(default=None),
):
    body = await request.body()

    if not _verify_signature(body, x_thrivecart_signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    result = process_webhook(payload, db)
    return {"ok": True, **result}


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok"}


@app.get("/subscriptions", tags=["Admin"])
def list_subscriptions(status: str | None = None, db: Session = Depends(get_db)):
    """List tracked subscriptions, optionally filtered by status."""
    from models import Subscription
    q = db.query(Subscription)
    if status:
        q = q.filter(Subscription.status == status)
    subs = q.order_by(Subscription.updated_at.desc()).all()
    return [
        {
            "id": s.thrivecart_subscription_id,
            "customer": s.customer_name,
            "email": s.customer_email,
            "product": s.product_name,
            "amount": s.amount,
            "status": s.status,
            "last_payment": s.last_payment_date,
            "next_payment_due": s.next_payment_date,
        }
        for s in subs
    ]


@app.get("/overdue", tags=["Admin"])
def list_overdue(db: Session = Depends(get_db)):
    """List subscriptions currently considered overdue (24h+ past due date)."""
    from datetime import timedelta
    from models import Subscription
    import os as _os
    from datetime import datetime

    hours = int(_os.getenv("OVERDUE_HOURS", "24"))
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    subs = (
        db.query(Subscription)
        .filter(
            Subscription.status == "active",
            Subscription.next_payment_date <= cutoff,
            Subscription.next_payment_date.isnot(None),
        )
        .all()
    )
    return [
        {
            "id": s.thrivecart_subscription_id,
            "customer": s.customer_name,
            "email": s.customer_email,
            "product": s.product_name,
            "amount": s.amount,
            "next_payment_due": s.next_payment_date,
        }
        for s in subs
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")), reload=False)
