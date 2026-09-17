from __future__ import annotations

from datetime import UTC, datetime

from app.reports.pdf import build_report_pdf, truncate_pdf_text

REPORT = {
    "month": "2024-05",
    "dashboard": {
        "salaryBase": 5000,
        "inflow": 5200,
        "outflow": 3100.5,
        "projectedBalance": 2099.5,
        "categoryBreakdown": [
            {"name": "Alimentação", "total": 800, "color": "#14B8A6"},
            {"name": "Transporte", "total": 420.75, "color": "#8B5CF6"},
        ],
        "paymentMethodBreakdown": [{"payment_method": "pix", "total": 900}],
        "monthlyTrend": [{"label": "Mai/24", "inflow": 5200, "outflow": 3100.5, "net": 2099.5}],
    },
    "goals": {"dailyGoal": 100, "recommendedDailyGoal": 90},
    "score": {"score": 78, "label": "Bom"},
    "categoryGrowth": {"hasHistory": False, "items": []},
    "alerts": [],
}
ROWS = [
    {
        "transaction_date": "2024-05-10",
        "type": "expense",
        "title": "Mercado maio",
        "category_name": "Alimentação",
        "payment_method": "credit",
        "amount": 150,
        "total_installments": None,
    }
]


def test_build_report_pdf_produces_valid_pdf_bytes():
    # DEP-08: gerador manual de bytes de PDF (objetos, xref, operadores de
    # desenho escritos à mão) trocado por fpdf2 — continua produzindo um PDF
    # válido, com o mesmo texto legível (stream sem compressão, ver
    # TrevoPdfReport.set_compression) que a suíte de integração
    # (tests/integration/test_export.py) já verificava.
    pdf_bytes = build_report_pdf(REPORT, ROWS, datetime(2024, 5, 31, 12, 0, tzinfo=UTC))

    assert pdf_bytes.startswith(b"%PDF")
    assert pdf_bytes.rstrip().endswith(b"%%EOF")
    assert len(pdf_bytes) > 1000

    text = pdf_bytes.decode("latin-1", errors="ignore")
    for expected in [
        "Trevo",
        "Relatório dashboard",
        "Resumo mensal",
        "Categorias",
        "Formas de pagamento",
        "Movimentações",
        "Mercado maio",
        "R$ 5.000,00",
    ]:
        assert expected in text


def test_build_report_pdf_paginates_long_transaction_lists():
    many_rows = ROWS * 60
    pdf_bytes = build_report_pdf(REPORT, many_rows, datetime(2024, 5, 31, 12, 0, tzinfo=UTC))
    text = pdf_bytes.decode("latin-1", errors="ignore")
    assert "Página 2" in text


def test_build_report_pdf_handles_empty_sections_without_crashing():
    empty_report = {
        **REPORT,
        "dashboard": {**REPORT["dashboard"], "categoryBreakdown": [], "paymentMethodBreakdown": [], "monthlyTrend": []},
        "alerts": [],
    }
    pdf_bytes = build_report_pdf(empty_report, [], datetime(2024, 5, 31, 12, 0, tzinfo=UTC))
    assert pdf_bytes.startswith(b"%PDF")
    text = pdf_bytes.decode("latin-1", errors="ignore")
    assert "Nenhuma movimentação encontrada para este mês." in text


def test_truncate_pdf_text_uses_ascii_ellipsis():
    # A fonte core do fpdf2 não cobre o caractere de reticências do cp1252.
    long_text = "a" * 50
    truncated = truncate_pdf_text(long_text, 10)
    assert truncated == "a" * 9 + "..."
    assert truncate_pdf_text("short", 10) == "short"
