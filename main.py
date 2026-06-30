import csv
import hashlib
import hmac
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import Subscription
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


def _find_col(headers: list[str], candidates: list[str]) -> str | None:
    """Find the first matching column name (case-insensitive)."""
    h = [c.strip().lower() for c in headers]
    for c in candidates:
        if c.lower() in h:
            return headers[h.index(c.lower())]
    return None


def _parse_date(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%b %d, %Y",
                "%B %d, %Y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
    return None


@app.post("/import-csv", tags=["Dashboard"])
async def import_csv(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")  # handles BOM from Excel exports
    except UnicodeDecodeError:
        text = content.decode("latin-1")

    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []

    email_col   = _find_col(headers, ["email", "customer email", "buyer email", "e-mail"])
    fname_col   = _find_col(headers, ["first name", "firstname", "buyer first name", "customer first name"])
    lname_col   = _find_col(headers, ["last name", "lastname", "buyer last name", "customer last name"])
    name_col    = _find_col(headers, ["name", "customer name", "full name", "buyer name"])
    product_col = _find_col(headers, ["product", "product name", "item name", "item"])
    amount_col  = _find_col(headers, ["amount", "revenue", "total", "price", "order total", "charge amount"])
    date_col    = _find_col(headers, ["date", "created at", "created", "order date", "transaction date", "payment date"])
    sub_id_col  = _find_col(headers, ["subscription id", "subscription_id", "sub id", "recurring id"])
    status_col  = _find_col(headers, ["status", "order status", "payment status"])

    if not email_col:
        return _render_dashboard(request, db, "Could not find an Email column in the CSV. Please check the file.", "error")

    imported = 0
    skipped  = 0
    seen: dict[str, dict] = {}  # subscription_id or email+product → best row

    for row in reader:
        email = row.get(email_col, "").strip().lower()
        if not email:
            skipped += 1
            continue

        status_val = row.get(status_col, "").strip().lower() if status_col else ""
        if status_val in ("refunded", "chargebacked", "disputed"):
            skipped += 1
            continue

        sub_id = row.get(sub_id_col, "").strip() if sub_id_col else ""
        product = row.get(product_col, "").strip() if product_col else ""
        key = sub_id if sub_id else f"{email}||{product}"

        date_val = _parse_date(row.get(date_col, "")) if date_col else None

        # Keep the most recent payment row per subscription
        if key not in seen or (date_val and seen[key]["date"] and date_val > seen[key]["date"]):
            seen[key] = {
                "email": email,
                "name": (
                    f"{row.get(fname_col,'').strip()} {row.get(lname_col,'').strip()}".strip()
                    if fname_col else row.get(name_col, "").strip() if name_col else ""
                ),
                "product": product,
                "sub_id": sub_id or f"imported-{email}-{product}".replace(" ", "-"),
                "amount": float(row.get(amount_col, 0) or 0) if amount_col else 0.0,
                "date": date_val,
                "status": status_val,
            }

    for key, data in seen.items():
        last_paid = data["date"]
        next_due  = (last_paid + timedelta(days=30)) if last_paid else None

        sub = db.query(Subscription).filter_by(
            thrivecart_subscription_id=data["sub_id"]
        ).first()

        if sub is None:
            sub = Subscription(
                thrivecart_subscription_id=data["sub_id"],
                customer_email=data["email"],
                customer_name=data["name"],
                product_name=data["product"],
                amount=data["amount"],
                status="active",
                last_payment_date=last_paid,
                next_payment_date=next_due,
            )
            db.add(sub)
        else:
            if last_paid and (sub.last_payment_date is None or last_paid > sub.last_payment_date):
                sub.last_payment_date = last_paid
                sub.next_payment_date = next_due
            sub.customer_name = data["name"] or sub.customer_name
            sub.product_name  = data["product"] or sub.product_name
            sub.amount        = data["amount"] or sub.amount

        imported += 1

    db.commit()
    msg = f"Successfully imported {imported} subscriber(s)."
    if skipped:
        msg += f" {skipped} row(s) skipped (refunds or missing email)."
    return _render_dashboard(request, db, msg, "success")


def _render_dashboard(request: Request, db: Session, message: str = "", message_type: str = "success"):
    cutoff   = datetime.utcnow() - timedelta(hours=OVERDUE_HOURS)
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
        "total":    len(all_subs),
        "active":   sum(1 for s in all_subs if s.status == "active" and not s.is_overdue),
        "overdue":  len(overdue),
        "inactive": sum(1 for s in all_subs if s.status in ("cancelled", "expired")),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request":      request,
        "subscriptions": all_subs,
        "overdue":      overdue,
        "stats":        stats,
        "now":          datetime.utcnow().strftime("%b %d, %Y at %H:%M UTC"),
        "message":      message,
        "message_type": message_type,
    })


@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
def dashboard(request: Request, db: Session = Depends(get_db)):
    return _render_dashboard(request, db)


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
