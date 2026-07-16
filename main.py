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
from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import SessionLocal, get_db, init_db
from email_service import send_overdue_alert
from models import Subscription
from scheduler import create_scheduler
from webhook import process_webhook

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Prevents multiple syncs running at the same time
_sync_running = False
_sync_status  = "idle"  # idle | running | done | error

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
    now    = datetime.utcnow()
    cutoff = now - timedelta(hours=OVERDUE_HOURS)  # 24h ago
    all_subs = db.query(Subscription).order_by(Subscription.updated_at.desc()).all()

    recurring  = [s for s in all_subs if (s.subscription_type or "recurring") == "recurring"]
    one_time   = [s for s in all_subs if (s.subscription_type or "recurring") == "one_time"]

    # Only chase unpaid whose due date passed within this window — older
    # missed payments are stale history, not active monitoring targets
    stale_cutoff = now - timedelta(days=int(os.getenv("UNPAID_MAX_DAYS", "120")))

    for s in recurring:
        due = s.next_payment_date
        trackable = due is not None and (
            s.status in ("active", "failed")
            or (s.status == "cancelled" and due <= now)
        )
        if trackable and due >= stale_cutoff:
            if due <= cutoff:
                s.payment_state = "overdue"
            elif due <= now:
                s.payment_state = "due"
            elif s.status == "failed":
                # ThriveCart reported a failed payment even though the due date
                # hasn't passed yet — they still owe money now
                s.payment_state = "due"
            else:
                s.payment_state = "paid"
        elif trackable:
            # unpaid, but the missed payment is older than the window
            s.payment_state = "stale"
        else:
            s.payment_state = s.status
        s.is_overdue = s.payment_state == "overdue"
        s.hours_overdue = (
            int((now - due).total_seconds() / 3600)
            if s.is_overdue and due else 0
        )

    overdue = [s for s in recurring if s.payment_state == "overdue"]
    due_now = [s for s in recurring if s.payment_state == "due"]

    active_count = sum(1 for s in recurring if s.payment_state == "paid")
    stats = {
        # Total = the sum of the visible cards: one-time + paid + due + overdue
        "total":     len(one_time) + active_count + len(due_now) + len(overdue),
        "recurring": len(recurring),
        "one_time":  len(one_time),
        "active":    active_count,
        "due":       len(due_now),
        "overdue":   len(overdue),
        "inactive":  sum(1 for s in all_subs if s.status in ("cancelled", "expired")),
    }

    return templates.TemplateResponse("dashboard.html", {
        "request":       request,
        "recurring":     recurring,
        "one_time":      one_time,
        "overdue":       overdue,
        "due_now":       due_now,
        "stats":         stats,
        "now":           now.strftime("%d %b %Y à %H:%M UTC"),
        "message":       message,
        "message_type":  message_type,
    })


@app.get("/", response_class=HTMLResponse, tags=["Dashboard"])
def dashboard(request: Request, db: Session = Depends(get_db)):
    return _render_dashboard(request, db)


@app.api_route("/test-email", methods=["GET", "POST"], response_class=HTMLResponse, tags=["Dashboard"])
async def test_email(request: Request, db: Session = Depends(get_db)):
    notify_email = os.getenv("NOTIFY_EMAIL", "")
    if not notify_email:
        return _render_dashboard(request, db,
            "NOTIFY_EMAIL n'est pas configuré dans Railway → Variables.", "error")
    try:
        await send_overdue_alert([{
            "customer_name": "Jean Dupont (test)",
            "customer_email": "test@example.com",
            "product_name": "Abonnement mensuel",
            "amount": 99.0,
            "next_payment_date": datetime.utcnow(),
        }])
        return _render_dashboard(request, db,
            f"Email de test envoyé avec succès à {notify_email}. Vérifiez votre boîte de réception (et les spams).", "success")
    except Exception as exc:
        logger.exception("Test email failed: %s", exc)
        return _render_dashboard(request, db,
            f"Échec de l'envoi : {exc}", "error")


@app.get("/debug-client", tags=["Debug"])
async def debug_client(q: str = ""):
    """Look up a client by (partial) email or name to see their stored state.
    Usage: /debug-client?q=jean"""
    if not q or len(q) < 3:
        return {"error": "Ajoutez ?q=<email ou nom> (3 caractères minimum)"}
    db = SessionLocal()
    try:
        pattern = f"%{q}%"
        rows = (
            db.query(Subscription)
            .filter(
                (Subscription.customer_email.ilike(pattern))
                | (Subscription.customer_name.ilike(pattern))
            )
            .limit(20)
            .all()
        )
        if not rows:
            return {"query": q, "found": 0,
                    "note": "Personne trouvée — ce client n'a pas été importé (transaction hors de la fenêtre de sync ?)"}
        return {
            "query": q,
            "found": len(rows),
            "clients": [
                {
                    "name": r.customer_name,
                    "email": r.customer_email,
                    "product": r.product_name,
                    "amount": r.amount,
                    "status": r.status,
                    "type": r.subscription_type,
                    "last_payment": str(r.last_payment_date),
                    "next_payment": str(r.next_payment_date),
                }
                for r in rows
            ],
        }
    finally:
        db.close()


@app.get("/debug-transactions", tags=["Debug"])
async def debug_transactions(email: str = "", pages: int = 10):
    """Fetch raw ThriveCart API transactions for one customer email.
    Shows exactly what the API returns (incl. status of declined attempts)."""
    api_key = os.getenv("THRIVECART_API_KEY", "")
    if not api_key:
        return {"error": "THRIVECART_API_KEY not set"}
    if not email:
        return {"error": "Ajoutez ?email=<adresse du client>"}

    email = email.strip().lower()
    matches = []
    statuses_seen = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for page in range(1, min(pages, 30) + 1):
            resp = await client.get(
                "https://thrivecart.com/api/external/transactions",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                params={"page": page, "limit": 100, "per_page": 100},
            )
            if resp.status_code == 429:
                await asyncio.sleep(15)
                continue
            if resp.status_code != 200:
                return {"error": f"API {resp.status_code}", "detail": resp.text[:300],
                        "matches_so_far": matches}
            rows = resp.json().get("transactions") or resp.json().get("data") or []
            if not rows:
                break
            for row in rows:
                st = str(row.get("status", "?")).lower()
                statuses_seen[st] = statuses_seen.get(st, 0) + 1
                row_email = str(
                    (row.get("customer") or {}).get("email") or row.get("email") or ""
                ).strip().lower()
                if row_email == email:
                    matches.append(row)
            await asyncio.sleep(1)

    return {
        "email": email,
        "pages_scanned": page,
        "all_statuses_seen_in_scan": statuses_seen,
        "matches": len(matches),
        "transactions": matches,
    }


@app.get("/debug-events", tags=["Debug"])
async def debug_events(q: str = "", limit: int = 30):
    """List received webhook events, optionally filtered by text (email, event type)."""
    from models import PaymentEvent
    db = SessionLocal()
    try:
        query = db.query(PaymentEvent).order_by(PaymentEvent.event_date.desc())
        if q:
            query = query.filter(PaymentEvent.raw_payload.ilike(f"%{q}%"))
        rows = query.limit(min(limit, 100)).all()
        return {
            "query": q,
            "found": len(rows),
            "events": [
                {
                    "date": str(r.event_date),
                    "event_type": r.event_type,
                    "subscription_id": r.thrivecart_subscription_id,
                    "amount": r.amount,
                    "payload_extract": (r.raw_payload or "")[:600],
                }
                for r in rows
            ],
        }
    finally:
        db.close()


_substatus_result: dict = {"state": "idle"}


async def _run_substatus_scan():
    """Scan ALL ThriveCart transactions and tally subscriptions by their
    current status as reported by ThriveCart — ground truth comparison."""
    global _substatus_result
    api_key = os.getenv("THRIVECART_API_KEY", "")
    sub_status: dict = {}   # subscription_id -> current status (newest row wins)
    one_time_rows = 0
    page = 0
    page_size = None
    oldest = newest = None
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while page < 200:
                resp = await client.get(
                    "https://thrivecart.com/api/external/transactions",
                    headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                    params={"page": page + 1, "limit": 100, "per_page": 100},
                )
                page += 1
                if resp.status_code == 429:
                    await asyncio.sleep(15)
                    page -= 1
                    continue
                if resp.status_code != 200:
                    break
                rows = resp.json().get("transactions") or resp.json().get("data") or []
                if not rows:
                    break
                if page_size is None:
                    page_size = len(rows)
                for row in rows:
                    ts = row.get("timestamp")
                    if ts:
                        d = datetime.utcfromtimestamp(ts)
                        oldest = d if oldest is None or d < oldest else oldest
                        newest = d if newest is None or d > newest else newest
                    sid = row.get("subscription_id")
                    if not sid:
                        one_time_rows += 1
                        continue
                    st = str(row.get("subscription_current_status") or "unknown").lower()
                    sub_status.setdefault(str(sid), st)  # newest-first: first wins
                _substatus_result = {"state": f"running (page {page})"}
                if len(rows) < (page_size or 1):
                    break
                await asyncio.sleep(2)

        tally: dict = {}
        for st in sub_status.values():
            tally[st] = tally.get(st, 0) + 1
        _substatus_result = {
            "state": "done",
            "pages_scanned": page,
            "date_range": f"{oldest} → {newest}",
            "distinct_subscriptions": len(sub_status),
            "by_thrivecart_status": dict(sorted(tally.items())),
            "one_time_transaction_rows": one_time_rows,
        }
    except Exception as exc:
        logger.exception("substatus scan failed: %s", exc)
        _substatus_result = {"state": "error", "error": str(exc)}


@app.get("/debug-substatus", tags=["Debug"])
async def debug_substatus(restart: int = 0):
    """Full-history tally of subscriptions by ThriveCart's own status.
    First call starts the scan; refresh to see progress/result."""
    global _substatus_result
    state = _substatus_result.get("state", "idle")
    if state == "idle" or (restart and not str(state).startswith("running")):
        _substatus_result = {"state": "running (starting)"}
        asyncio.get_event_loop().create_task(_run_substatus_scan())
        return {"state": "started — refresh this page in a minute"}
    return _substatus_result


@app.get("/debug-db", tags=["Debug"])
async def debug_db():
    """Shows which database is in use and row counts."""
    from database import DATABASE_URL
    from scheduler import _find_unpaid
    db = SessionLocal()
    try:
        total = db.query(Subscription).count()
        unpaid = _find_unpaid(db)
        db_kind = "PostgreSQL (persistent)" if DATABASE_URL.startswith("postgresql") else "SQLite (EPHEMERAL — data lost on each redeploy!)"

        now = datetime.utcnow()
        stale_cutoff = now - timedelta(days=int(os.getenv("UNPAID_MAX_DAYS", "120")))
        recurring = [
            s for s in db.query(Subscription).all()
            if (s.subscription_type or "recurring") == "recurring"
        ]
        buckets: dict = {}
        for s in recurring:
            due = s.next_payment_date
            if s.status != "active":
                key = f"status_{s.status}"
            elif due is None:
                key = "active_no_due_date"
            elif due > now:
                key = "active_paid_future_due"
            elif due >= stale_cutoff:
                key = "active_overdue_within_120d"
            else:
                key = "active_stale_older_120d"
            buckets[key] = buckets.get(key, 0) + 1

        return {
            "database": db_kind,
            "total_subscriptions": total,
            "unpaid_count": len(unpaid),
            "total_unpaid": f"{sum(u['amount'] for u in unpaid):.2f} $",
            "recurring_breakdown": dict(sorted(buckets.items())),
        }
    finally:
        db.close()


@app.get("/report.pdf", tags=["Dashboard"])
async def report_pdf():
    """Download the current unpaid report as a PDF."""
    from scheduler import _find_unpaid
    from pdf_service import build_unpaid_pdf
    from fastapi.responses import Response
    db = SessionLocal()
    try:
        unpaid = _find_unpaid(db)
        pdf_bytes = build_unpaid_pdf(unpaid, "")
        filename = f"rapport-impayes-{datetime.utcnow().strftime('%Y-%m-%d')}.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    finally:
        db.close()


@app.get("/test-all-emails", tags=["Debug"])
async def test_all_emails():
    """Showcase: sends the weekly report, the June monthly report, and a demo
    of the client 24h-overdue reminder — all delivered to NOTIFY_EMAIL."""
    from scheduler import _find_unpaid
    from email_service import send_unpaid_report, send_client_reminder
    notify = os.getenv("NOTIFY_EMAIL", "")
    results = {}
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        unpaid = _find_unpaid(db)

        try:
            week_since = now - timedelta(days=7)
            weekly = _find_unpaid(db, since=week_since, until=now)
            label = f"hebdomadaire (du {week_since.strftime('%d/%m')} au {now.strftime('%d/%m/%Y')})"
            await send_unpaid_report(weekly, label)
            results["rapport_hebdomadaire"] = f"SENT ({len(weekly)} impayés cette semaine)"
        except Exception as exc:
            results["rapport_hebdomadaire"] = f"FAILED: {exc}"

        try:
            june_start = datetime(2026, 6, 1)
            june_end = datetime(2026, 7, 1)
            june = _find_unpaid(db, since=june_start, until=june_end)
            await send_unpaid_report(june, "mensuel — juin 2026")
            results["rapport_mensuel_juin"] = f"SENT ({len(june)} impayés en juin)"
        except Exception as exc:
            results["rapport_mensuel_juin"] = f"FAILED: {exc}"

        try:
            sample = (
                {
                    "customer_name": unpaid[0]["customer_name"],
                    "customer_email": unpaid[0]["customer_email"],
                    "product_name": unpaid[0]["product_name"],
                    "amount": unpaid[0]["amount"],
                    "next_payment_date": unpaid[0]["next_payment_date"],
                }
                if unpaid
                else {
                    "customer_name": "Jean Dupont",
                    "customer_email": "client@example.com",
                    "product_name": "Abonnement mensuel",
                    "amount": 99.0,
                    "next_payment_date": datetime.utcnow() - timedelta(hours=30),
                }
            )
            # Demo: delivered to NOTIFY_EMAIL, NOT to the real client
            await send_client_reminder(sample, recipient_override=notify)
            results["rappel_client_24h"] = "SENT (démo, livré à vous — pas au client)"
        except Exception as exc:
            results["rappel_client_24h"] = f"FAILED: {exc}"

        return {"sent_to": notify, "unpaid_count": len(unpaid), "results": results}
    finally:
        db.close()


@app.get("/test-report", tags=["Debug"])
async def test_report():
    """Manually trigger the weekly unpaid report email (for testing)."""
    from scheduler import _find_unpaid
    from email_service import send_unpaid_report
    db = SessionLocal()
    try:
        unpaid = _find_unpaid(db)
        await send_unpaid_report(unpaid, "hebdomadaire (test)")
        total = sum(u["amount"] for u in unpaid)
        return {
            "result": "SUCCESS",
            "unpaid_count": len(unpaid),
            "total_unpaid": f"{total:.2f} $",
            "sent_to": os.getenv("NOTIFY_EMAIL", ""),
        }
    except Exception as exc:
        logger.exception("test_report failed: %s", exc)
        return {"result": "FAILED", "error": str(exc)}
    finally:
        db.close()


@app.get("/debug-email", tags=["Debug"])
async def debug_email():
    """Returns env var status and attempts to send a test email. Visit this URL directly."""
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = os.getenv("SMTP_PORT", "587")
    notify_email = os.getenv("NOTIFY_EMAIL", "")
    brevo_key = os.getenv("BREVO_API_KEY", "")

    config_status = {
        "BREVO_API_KEY": "SET (hidden)" if brevo_key else "NOT SET",
        "SMTP_HOST":     smtp_host,
        "SMTP_PORT":     smtp_port,
        "SMTP_USER":     smtp_user if smtp_user else "NOT SET",
        "SMTP_PASS":     "SET (hidden)" if smtp_pass else "NOT SET",
        "NOTIFY_EMAIL":  notify_email if notify_email else "NOT SET",
    }

    if not notify_email or (not brevo_key and (not smtp_user or not smtp_pass)):
        return {"config": config_status, "result": "FAILED", "error": "Missing env vars — see config above"}

    try:
        await send_overdue_alert([{
            "customer_name": "Test debug",
            "customer_email": "test@example.com",
            "product_name":  "Debug test",
            "amount":        0.0,
            "next_payment_date": datetime.utcnow(),
        }])
        return {"config": config_status, "result": "SUCCESS", "sent_to": notify_email}
    except Exception as exc:
        logger.exception("debug_email failed: %s", exc)
        return {"config": config_status, "result": "FAILED", "error": str(exc)}


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
        or row.get("customer", {}).get("name", "")
    )
    product_name = product.get("name") or row.get("item_name") or row.get("product_name", "")
    product_id   = str(product.get("id") or row.get("item_id") or row.get("product_id", ""))
    raw_sub_id   = row.get("subscription_id") or order.get("subscription_id")
    sub_type     = "recurring" if raw_sub_id else "one_time"
    sub_id       = str(
        raw_sub_id or row.get("order_id") or row.get("id") or f"api-{email}-{product_id}"
    )
    amount_raw = row.get("amount") or order.get("order_total") or row.get("revenue") or 0
    try:
        # ThriveCart returns amounts in cents (e.g. 25000 = $250.00)
        amount = float(str(amount_raw).replace("$", "").replace(",", "")) / 100
    except (ValueError, TypeError):
        amount = 0.0

    # Prefer Unix timestamp, fall back to date string
    ts = row.get("timestamp") or row.get("created") or row.get("created_at")
    if isinstance(ts, (int, float)):
        last_paid = datetime.utcfromtimestamp(ts)
    else:
        date_raw = row.get("date") or row.get("time") or row.get("created_at")
        last_paid = _parse_date(date_raw) if isinstance(date_raw, str) else None

    next_due = (last_paid + timedelta(days=30)) if last_paid else None

    # transaction_type tells what happened: rebill/purchase = money moved,
    # cancel/refund = NO payment. Declined rebills never appear in the API.
    tx_type = str(row.get("transaction_type") or "").lower()
    is_payment = amount > 0 and tx_type not in ("cancel", "cancellation", "refund", "rebill_failed", "failed")

    # subscription_current_status is the authoritative CURRENT state
    sub_status_raw = str(row.get("subscription_current_status") or row.get("status") or "").lower()
    if sub_status_raw in ("cancelled", "canceled", "refunded"):
        status = "cancelled"
    elif sub_status_raw in ("completed", "complete", "expired", "finished"):
        status = "expired"
    elif sub_status_raw in ("past_due", "pastdue", "failed", "delinquent"):
        status = "failed"
    elif sub_status_raw in ("paused", "pause"):
        status = "paused"
    else:
        status = "active"

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
            subscription_type=sub_type,
            last_payment_date=last_paid if is_payment else None,
            next_payment_date=next_due if is_payment else None,
        )
        db.add(sub)
        db.flush()
    else:
        if not is_payment and last_paid and sub.last_payment_date == last_paid:
            # Repair: an earlier sync wrongly recorded this cancel/refund
            # transaction as a payment — undo so real payments can refill
            sub.last_payment_date = None
            sub.next_payment_date = None
        if is_payment and last_paid and (sub.last_payment_date is None or last_paid > sub.last_payment_date):
            sub.last_payment_date = last_paid
            if sub_type == "recurring":
                sub.next_payment_date = next_due
        sub.status             = status
        sub.customer_name      = name or sub.customer_name
        sub.product_name       = product_name or sub.product_name
        sub.amount             = max(amount, sub.amount or 0)
        sub.subscription_type  = sub_type  # upgrade to recurring if a sub_id appears later
    return is_new


async def _run_sync_background():
    """Runs the full ThriveCart sync in the background."""
    global _sync_running, _sync_status
    _sync_running = True
    _sync_status  = "running"
    api_key = os.getenv("THRIVECART_API_KEY", "")
    imported = updated = page = 0
    page_size = None
    # Import window: without enough history, subscribers whose card started
    # declining months ago have no recent successful transaction and would
    # never be imported (declined rebills don't appear in the API at all).
    sync_days = int(os.getenv("SYNC_DAYS", "365"))
    oldest_allowed = datetime.utcnow() - timedelta(days=sync_days)

    db = SessionLocal()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                try:
                    resp = await client.get(
                        "https://thrivecart.com/api/external/transactions",
                        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                        params={"page": page + 1, "limit": 100, "per_page": 100},
                    )
                    page += 1
                    logger.info("Sync page %d — status %d", page, resp.status_code)

                    if resp.status_code == 429:
                        logger.warning("Rate limited — waiting 15s")
                        await asyncio.sleep(15)
                        page -= 1  # retry same page
                        continue
                    if resp.status_code != 200:
                        logger.error("API error %d: %s", resp.status_code, resp.text[:200])
                        break

                    rows = resp.json().get("transactions") or resp.json().get("data") or []
                    if not rows:
                        break

                    if page_size is None:
                        page_size = len(rows)

                    stop_early = False
                    for row in rows:
                        ts = row.get("timestamp")
                        row_date = datetime.utcfromtimestamp(ts) if ts else None
                        if row_date and row_date < oldest_allowed:
                            stop_early = True
                            continue
                        is_new = _upsert_from_api_row(db, row)
                        if is_new:
                            imported += 1
                        else:
                            updated += 1

                    db.commit()

                    if stop_early or len(rows) < (page_size or 1):
                        break

                    await asyncio.sleep(2)

                except Exception as exc:
                    logger.exception("Sync error page %d: %s", page, exc)
                    db.rollback()
                    break

        _sync_status = f"done:{imported}:{updated}:{page}"
        logger.info("Sync complete — %d new, %d updated across %d pages", imported, updated, page)
    finally:
        db.close()
        _sync_running = False


@app.api_route("/sync-thrivecart", methods=["GET", "POST"], response_class=HTMLResponse, tags=["Dashboard"])
async def sync_thrivecart(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    global _sync_running, _sync_status
    api_key = os.getenv("THRIVECART_API_KEY", "")
    if not api_key:
        return _render_dashboard(request, db,
            "THRIVECART_API_KEY is not set. Add it in Railway → Variables.", "error")

    if _sync_running:
        return _render_dashboard(request, db,
            "Sync already in progress — check back in a minute. The dashboard will update automatically.", "success")

    if _sync_status.startswith("done:"):
        _, imp, upd, pages = _sync_status.split(":")
        msg = f"Last sync imported {imp} new subscriber(s), updated {upd}, across {pages} page(s)."
        _sync_status = "idle"
        return _render_dashboard(request, db, msg, "success")

    background_tasks.add_task(_run_sync_background)
    _sync_running = True
    _sync_status  = "running"
    return _render_dashboard(request, db,
        "Sync started in the background. This page refreshes automatically every 60 seconds — your subscribers will appear shortly.", "success")


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
