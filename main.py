import asyncio
import csv
import hashlib
import hmac
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import httpx
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


def _upsert_from_api_row(db: Session, row: dict) -> bool:
    """Save one transaction row from ThriveCart API into the DB. Returns True if new."""
    customer = row.get("customer", {})
    product  = row.get("product", {})
    order    = row.get("order", row)  # some responses put fields at root level

    email = (customer.get("email") or row.get("email") or "").strip().lower()
    if not email:
        return False

    name = (
        f"{customer.get('firstname', '')} {customer.get('lastname', '')}".strip()
        or customer.get("name", "")
        or row.get("customer_name", "")
    )
    product_name = product.get("name") or row.get("product_name", "")
    product_id   = str(product.get("id") or row.get("product_id", ""))
    sub_id       = str(
        row.get("subscription_id") or order.get("subscription_id")
        or row.get("id") or row.get("order_id") or f"api-{email}-{product_id}"
    )
    amount_raw = row.get("amount") or order.get("order_total") or row.get("revenue") or 0
    try:
        amount = float(str(amount_raw).replace("$", "").replace(",", ""))
    except (ValueError, TypeError):
        amount = 0.0

    date_raw = row.get("created") or row.get("created_at") or row.get("date")
    if isinstance(date_raw, (int, float)):
        last_paid = datetime.utcfromtimestamp(date_raw)
    elif isinstance(date_raw, str):
        last_paid = _parse_date(date_raw)
    else:
        last_paid = None

    next_due = (last_paid + timedelta(days=30)) if last_paid else None

    status_raw = (row.get("status") or "").lower()
    status = "cancelled" if status_raw in ("cancelled", "canceled", "refunded") else "active"

    sub = db.query(Subscription).filter_by(thrivecart_subscription_id=sub_id).first()
    is_new = sub is None
    if is_new:
        sub = Subscription(
            thrivecart_subscription_id=sub_id,
            customer_email=email,
            customer_name=name,
            product_name=product_name,
            product_id=product_id,
            amount=amount,
            status=status,
            last_payment_date=last_paid,
            next_payment_date=next_due,
        )
        db.add(sub)
        db.flush()  # write to DB within transaction so duplicates on later pages are found
    else:
        if last_paid and (sub.last_payment_date is None or last_paid > sub.last_payment_date):
            sub.last_payment_date = last_paid
            sub.next_payment_date = next_due
        sub.customer_name = name or sub.customer_name
        sub.product_name  = product_name or sub.product_name
        sub.amount        = amount or sub.amount
    return is_new


@app.api_route("/sync-thrivecart", methods=["GET", "POST"], response_class=HTMLResponse, tags=["Dashboard"])
async def sync_thrivecart(request: Request, db: Session = Depends(get_db)):
    api_key = os.getenv("THRIVECART_API_KEY", "")
    if not api_key:
        return _render_dashboard(request, db,
            "THRIVECART_API_KEY is not set. Add it in Railway → Variables.", "error")

    imported   = 0
    updated    = 0
    page       = 1
    page_size  = None   # detected from first response
    errors     = []
    four_months_ago = datetime.utcnow() - timedelta(days=120)

    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            try:
                resp = await client.get(
                    "https://thrivecart.com/api/external/transactions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Accept": "application/json",
                    },
                    params={
                        "page": page,
                        "limit": 100,
                        "per_page": 100,
                    },
                )
                logger.info("ThriveCart API page %d — status %d — body: %s",
                            page, resp.status_code, resp.text[:800])

                if resp.status_code == 401:
                    return _render_dashboard(request, db,
                        "Invalid API key. Check THRIVECART_API_KEY in Railway Variables.", "error")
                if resp.status_code == 404:
                    return _render_dashboard(request, db,
                        "ThriveCart API endpoint not found — check Railway logs for the raw response.", "error")
                if resp.status_code == 429:
                    logger.warning("Rate limited by ThriveCart on page %d — waiting 10s", page)
                    await asyncio.sleep(10)
                    continue  # retry same page
                if resp.status_code != 200:
                    return _render_dashboard(request, db,
                        f"ThriveCart API returned {resp.status_code}: {resp.text[:300]}", "error")

                data = resp.json()

                # Handle different response shapes ThriveCart might return
                rows = (
                    data.get("transactions")
                    or data.get("data")
                    or data.get("orders")
                    or data.get("results")
                    or (data if isinstance(data, list) else [])
                )

                if not rows:
                    break

                # Detect actual page size from first response
                if page_size is None:
                    page_size = len(rows)

                # Filter to last 4 months
                for row in rows:
                    date_raw = row.get("created") or row.get("created_at") or row.get("date")
                    if isinstance(date_raw, (int, float)):
                        row_date = datetime.utcfromtimestamp(date_raw)
                    elif isinstance(date_raw, str):
                        row_date = _parse_date(date_raw)
                    else:
                        row_date = None

                    if row_date and row_date < four_months_ago:
                        continue  # skip older than 4 months

                    is_new = _upsert_from_api_row(db, row)
                    if is_new:
                        imported += 1
                    else:
                        updated += 1

                db.commit()

                # Stop only when we get an empty page or fewer than detected page size
                if len(rows) < (page_size or 1):
                    break
                page += 1
                await asyncio.sleep(2)  # respect ThriveCart rate limits

            except Exception as exc:
                logger.exception("API sync error on page %d: %s", page, exc)
                db.rollback()
                errors.append(str(exc))
                break

    if errors:
        return _render_dashboard(request, db,
            f"Sync completed with errors: {errors[0]}", "error")

    msg = (f"Sync complete — {imported} new subscriber(s) added, "
           f"{updated} updated. Data covers the last 4 months ({page - 1} page(s) fetched).")
    return _render_dashboard(request, db, msg, "success")


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
