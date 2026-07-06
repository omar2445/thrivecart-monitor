"""Generates the unpaid-payments report as a PDF."""

from datetime import datetime
from fpdf import FPDF


def _txt(value) -> str:
    """fpdf2 core fonts are latin-1 only — replace unsupported characters."""
    return str(value or "").encode("latin-1", "replace").decode("latin-1")


def build_unpaid_pdf(unpaid_list: list[dict], period_label: str) -> bytes:
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    # Title
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(26, 26, 46)
    title = "Rapport des paiements impayés"
    if period_label:
        title += f" ({period_label})"
    pdf.cell(0, 10, _txt(title), ln=True)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 7, _txt(f"Généré le {datetime.utcnow().strftime('%d/%m/%Y à %H:%M UTC')}"), ln=True)
    pdf.ln(4)

    count = len(unpaid_list)
    total = sum(s["amount"] or 0.0 for s in unpaid_list)

    if count == 0:
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(39, 103, 73)
        pdf.cell(0, 10, _txt("Tous les clients ont payé. Aucun impayé à signaler."), ln=True)
        return bytes(pdf.output())

    # Summary line
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(192, 57, 43)
    pdf.cell(0, 8, _txt(f"{count} client(s) impayé(s) - Total : {total:.2f} $"), ln=True)
    pdf.ln(3)

    # Table header
    col_widths = [55, 75, 70, 30, 35]
    headers = ["Nom", "Email", "Produit", "Montant", "Échéance"]

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(242, 242, 242)
    pdf.set_text_color(60, 60, 60)
    for w, h in zip(col_widths, headers):
        pdf.cell(w, 8, _txt(h), border=1, fill=True)
    pdf.ln()

    # Rows
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(40, 40, 40)
    for sub in unpaid_list:
        due = sub["next_payment_date"]
        due_str = due.strftime("%d/%m/%Y") if isinstance(due, datetime) else str(due or "-")
        cells = [
            sub.get("customer_name") or "-",
            sub.get("customer_email") or "-",
            sub.get("product_name") or "-",
            f"{(sub.get('amount') or 0.0):.2f} $",
            due_str,
        ]
        for w, value in zip(col_widths, cells):
            text = _txt(value)
            # Truncate to fit the column
            while pdf.get_string_width(text) > w - 3 and len(text) > 3:
                text = text[:-4] + "..."
            pdf.cell(w, 7, text, border=1)
        pdf.ln()

    # Total row
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(255, 245, 245)
    pdf.set_text_color(192, 57, 43)
    pdf.cell(sum(col_widths[:3]), 9, _txt("TOTAL IMPAYÉ"), border=1, fill=True)
    pdf.cell(col_widths[3], 9, _txt(f"{total:.2f} $"), border=1, fill=True)
    pdf.cell(col_widths[4], 9, "", border=1, fill=True)
    pdf.ln()

    return bytes(pdf.output())
