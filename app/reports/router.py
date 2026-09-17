from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends, Request, Response

from app.api.deps import PlainDictRoute, enforce_ip_rate_limit, get_current_user, limiter
from app.imports.parsers.csv import csv_safe_cell
from app.reports.pdf import build_report_pdf
from app.reports.service import get_export_transactions, get_reports_summary, payment_method_label, transaction_source_label
from app.shared import clock
from app.shared.dates import get_current_month
from app.shared.money import round_money
from app.shared.validation import validate_month_text

router = APIRouter(route_class=PlainDictRoute)


@router.get("/api/reports")
def reports(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    return get_reports_summary(current_user["id"], month_key)



@router.get("/api/export/csv")
@limiter.limit("20 per 1 hour")
def export_csv(request: Request, month: str | None = None, current_user: dict = Depends(get_current_user)) -> Response:
    enforce_ip_rate_limit(request, "export_csv", max_attempts=20, window_seconds=3600)
    user_id = current_user["id"]
    month_key = validate_month_text(month) or get_current_month()
    rows = get_export_transactions(user_id, month_key)

    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(
        [
            "Data",
            "Tipo",
            "Nome",
            "Categoria",
            "Forma de pagamento",
            "Valor",
            "Origem",
            "Observa\u00e7\u00f5es",
            "Parcela",
        ]
    )
    for row in rows:
        installment = ""
        if row.get("total_installments"):
            installment = f"{row.get('installment_number')}/{row.get('total_installments')}"
        transaction_type = "Entrada" if row.get("type") == "income" else "Despesa"
        writer.writerow(
            [
                row.get("transaction_date") or "",
                transaction_type,
                csv_safe_cell(row.get("title")),
                csv_safe_cell(row.get("category_name")),
                csv_safe_cell(payment_method_label(row.get("payment_method"))),
                f"{round_money(row.get('amount') or 0):.2f}".replace(".", ","),
                csv_safe_cell(transaction_source_label(row.get("source"))),
                csv_safe_cell(row.get("notes")),
                csv_safe_cell(installment),
            ]
        )

    headers = {
        "Content-Disposition": f'attachment; filename="trevo-relatorio-{month_key}.csv"',
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }
    return Response(content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)



@router.get("/api/export/pdf")
@limiter.limit("20 per 1 hour")
def export_pdf(request: Request, month: str | None = None, current_user: dict = Depends(get_current_user)) -> Response:
    enforce_ip_rate_limit(request, "export_pdf", max_attempts=20, window_seconds=3600)
    user_id = current_user["id"]
    month_key = validate_month_text(month) or get_current_month()
    report = get_reports_summary(user_id, month_key)
    rows = get_export_transactions(user_id, month_key)
    headers = {
        "Content-Disposition": f'attachment; filename="trevo-relatorio-{month_key}.pdf"',
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    }
    return Response(
        # DOM-04: horário local de exibição, não UTC — "Gerado em" perto da
        # meia-noite mostrava um dia adiantado para quem lê em horário do
        # Brasil.
        content=build_report_pdf(report, rows, clock.now()),
        media_type="application/pdf",
        headers=headers,
    )

