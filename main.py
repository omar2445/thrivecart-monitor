import hashlib
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
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
OVERDUE_HOURS = int(os.getenv("OVERDUE_HOURS", "24"))

templates = Jinja2Templates(directory="templates")


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


@app.api_route("/webhook/thrivecart", methods=["GET", "POST", "HEAD"], tags=["Webhooks"])
async def thrivecart_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    if request.method in ("GET", "HEAD"):
        return {"ok": True}

    try:
        body = await request.body()
        logger.info("Webhook received — method: %s  bytes: %d  body: %s",
                    request.method, len(body), body[:300])

        if not body or body.strip() in (b"", b"{}", b"[]"):
            return {"ok": True}

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            logger.warning("Non-JSON webhook body (form-encoded?): %s", body[:300])
            return {"ok": True}

        result = process_webhook(payload, db)
        return {"ok": True, **result}

    except Exception as exc:
        logger.exception("Unhandled error in webhook: %s", exc)
        return {"ok": True, "error": str(exc)}


@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
def dashboard(request: Request, db: Session = Depends(get_db)):
    from models import Subscription
    cutoff = datetime.utcnow() - timedelta(hours=OVERDUE_HOURS)

    all_subs = db.query(Subscription).order_by(Subscription.updated_at.desc()).all()

    for s in all_subs:
        s.is_overdue = (
            s.status == "active"
            and s.next_payment_date is not None
            and s.next_payment_date <= cutoff
        )
        s.days_overdue = (
            (datetime.utcnow() - s.next_payment_date).days
            if s.is_overdue and s.next_payment_date else 0
        )

    overdue = [s for s in all_subs if s.is_overdue]

    stats = {
        "total": len(all_subs),
        "active": sum(1 for s in all_subs if s.status == "active" and not s.is_overdue),
        "overdue": len(overdue),
        "inactive": sum(1 for s in all_subs if s.status in ("cancelled", "expired")),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "subscriptions": all_subs,
        "overdue": overdue,
        "stats": stats,
        "now": datetime.utcnow().strftime("%b %d, %Y at %H:%M UTC"),
    })


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
