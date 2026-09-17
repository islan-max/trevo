from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fpdf import FPDF

from app.reports.service import payment_method_label
from app.shared.money import format_brl, round_money, to_decimal

PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN = 36


def _rgb(hex_color: str) -> tuple[int, int, int]:
    cleaned = str(hex_color or "#102033").strip().lstrip("#")
    if len(cleaned) != 6:
        cleaned = "102033"
    try:
        return int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16)
    except ValueError:
        return _rgb("#102033")


def _sanitize(value: Any) -> str:
    # DEP-08: a fonte core Helvetica só cobre latin-1 de verdade (sem
    # travessão/reticências do cp1252) — mesmo comportamento do gerador
    # anterior, que fazia .encode("latin-1", "replace") e virava "?" pra
    # qualquer coisa fora do intervalo, em vez de derrubar a geração do PDF.
    return str(value or "").encode("latin-1", "replace").decode("latin-1")


def truncate_pdf_text(value: Any, max_length: int) -> str:
    text = str(value or "")
    # "..." em vez de "…": o caractere de reticências do cp1252 não existe na
    # fonte core Helvetica do fpdf2 (só cobre latin-1 puro).
    return text if len(text) <= max_length else text[: max_length - 1] + "..."


class TrevoPdfReport(FPDF):
    width = PAGE_WIDTH
    height = PAGE_HEIGHT
    margin = MARGIN

    def __init__(self) -> None:
        super().__init__(orientation="P", unit="pt", format=(self.width, self.height))
        self.set_margins(self.margin, self.margin, self.margin)
        # Paginação decidida à mão por ensure_space(), igual ao gerador
        # anterior — o auto page break do fpdf2 cortaria no meio de um
        # cartão/barra desenhado por várias chamadas seguidas.
        self.set_auto_page_break(auto=False)
        # tests/integration/test_export.py verifica o texto do relatório lendo
        # os bytes crus do PDF (decode latin-1) — sem isso, o stream vem
        # comprimido com zlib e o teste não encontra mais nada.
        self.set_compression(False)
        self.set_font("Helvetica", size=10)
        self.add_page()

    def ensure_space(self, height: float) -> None:
        if self.y + height > self.height - 58:
            self.add_page()

    def fill_rect(self, x: float, y: float, width: float, height: float, fill: str = "#FFFFFF", stroke: str | None = None) -> None:
        self.set_fill_color(*_rgb(fill))
        if stroke:
            self.set_draw_color(*_rgb(stroke))
            self.rect(x, y, width, height, style="FD")
        else:
            self.rect(x, y, width, height, style="F")

    def draw_line(self, x1: float, y1: float, x2: float, y2: float, color: str = "#DDE7F0", width: float = 1) -> None:
        self.set_draw_color(*_rgb(color))
        self.set_line_width(width)
        self.line(x1, y1, x2, y2)

    def write_text(self, x: float, y: float, value: Any, size: int = 10, color: str = "#102033", bold: bool = False) -> None:
        self.set_font("Helvetica", style="B" if bold else "", size=size)
        self.set_text_color(*_rgb(color))
        self.text(x, y, _sanitize(value))

    def footer(self) -> None:
        # write_text()/draw_line() (via FPDF.text()/line()) medem y a partir
        # do TOPO da página — o rodapé fica a 42/24pt da BASE, então precisa
        # da distância a partir do topo (self.height - N), não N direto.
        footer_line_y = self.height - 42
        footer_text_y = self.height - 24
        self.draw_line(self.margin, footer_line_y, self.width - self.margin, footer_line_y, "#DDE7F0")
        self.write_text(self.margin, footer_text_y, "Trevo", size=9, color="#6D7B8D", bold=True)
        self.write_text(self.width - 96, footer_text_y, f"Página {self.page_no()}", size=9, color="#6D7B8D")

    def build(self) -> bytes:
        return bytes(self.output())


def add_pdf_section(pdf: TrevoPdfReport, title: str, description: str | None = None) -> None:
    pdf.ensure_space(42)
    pdf.write_text(pdf.margin, pdf.y, title, size=14, color="#102033", bold=True)
    pdf.y += 16
    if description:
        pdf.write_text(pdf.margin, pdf.y, description, size=9, color="#6D7B8D")
        pdf.y += 14
    pdf.draw_line(pdf.margin, pdf.y, pdf.width - pdf.margin, pdf.y, "#DDE7F0")
    pdf.y += 14


def add_pdf_summary_cards(pdf: TrevoPdfReport, cards: list[tuple[str, str, str]]) -> None:
    card_gap = 8
    card_width = (pdf.width - pdf.margin * 2 - card_gap * 4) / 5
    card_height = 58
    pdf.ensure_space(card_height + 12)
    for index, (label, value, tone) in enumerate(cards):
        x = pdf.margin + index * (card_width + card_gap)
        pdf.fill_rect(x, pdf.y, card_width, card_height, fill=tone, stroke="#DDE7F0")
        pdf.write_text(x + 8, pdf.y + 16, label, size=7, color="#6D7B8D", bold=True)
        pdf.write_text(x + 8, pdf.y + 38, value, size=10, color="#102033", bold=True)
    pdf.y += card_height + 16


def add_pdf_bar_rows(
    pdf: TrevoPdfReport,
    rows: list[dict],
    label_key: str,
    value_key: str,
    empty_text: str,
    color_key: str | None = None,
    limit: int = 8,
) -> None:
    if not rows:
        pdf.ensure_space(22)
        pdf.write_text(pdf.margin, pdf.y, empty_text, size=9, color="#6D7B8D")
        pdf.y += 20
        return

    visible_rows = rows[:limit]
    max_value = max([abs(to_decimal(row.get(value_key) or 0)) for row in visible_rows] + [Decimal("1")])
    for row in visible_rows:
        pdf.ensure_space(31)
        value = round_money(row.get(value_key) or 0)
        label = truncate_pdf_text(row.get(label_key) or "Sem categoria", 30)
        pdf.write_text(pdf.margin, pdf.y + 8, label, size=9, color="#102033", bold=True)
        pdf.write_text(pdf.width - pdf.margin - 96, pdf.y + 8, format_brl(value), size=9, color="#102033")
        bar_x = pdf.margin
        bar_y = pdf.y + 15
        bar_width = pdf.width - pdf.margin * 2
        pdf.fill_rect(bar_x, bar_y, bar_width, 8, fill="#EEF5F8")
        filled_width = float(abs(value) / max_value) * bar_width if max_value > 0 else 0
        fill_color = row.get(color_key or "") or ("#E14B5A" if value > 0 and value_key == "delta" else "#14B8A6")
        if value_key == "delta" and value < 0:
            fill_color = "#18A957"
        pdf.fill_rect(bar_x, bar_y, max(2, filled_width), 8, fill=fill_color)
        pdf.y += 31


def add_pdf_table(pdf: TrevoPdfReport, headers: list[str], rows: list[list[str]], widths: list[float], empty_text: str) -> None:
    if not rows:
        pdf.ensure_space(22)
        pdf.write_text(pdf.margin, pdf.y, empty_text, size=9, color="#6D7B8D")
        pdf.y += 20
        return

    def draw_header() -> None:
        pdf.fill_rect(pdf.margin, pdf.y, sum(widths), 22, fill="#EEF5F8", stroke="#DDE7F0")
        x = pdf.margin + 6
        for index, header in enumerate(headers):
            pdf.write_text(x, pdf.y + 14, header, size=8, color="#102033", bold=True)
            x += widths[index]
        pdf.y += 22

    draw_header()
    for row in rows:
        pdf.ensure_space(24)
        if pdf.y < 60:
            draw_header()
        pdf.draw_line(pdf.margin, pdf.y, pdf.margin + sum(widths), pdf.y, "#DDE7F0")
        x = pdf.margin + 6
        for index, cell in enumerate(row):
            pdf.write_text(x, pdf.y + 15, truncate_pdf_text(cell, max(10, int(widths[index] / 4.8))), size=8, color="#102033")
            x += widths[index]
        pdf.y += 24
    pdf.y += 8


def build_report_pdf(report: dict, rows: list[dict], generated_at: datetime) -> bytes:
    pdf = TrevoPdfReport()
    dashboard = report["dashboard"]
    goals = report["goals"]
    score = report["score"]
    category_rows = dashboard.get("categoryBreakdown") or []
    payment_rows = dashboard.get("paymentMethodBreakdown") or []
    trend_rows = (dashboard.get("monthlyTrend") or [])[-6:]
    growth = report.get("categoryGrowth") or {"hasHistory": False, "items": []}

    pdf.fill_rect(0, 0, pdf.width, 92, fill="#0A1728")
    pdf.write_text(pdf.margin, 34, "Trevo", size=23, color="#FFFFFF", bold=True)
    pdf.write_text(pdf.margin, 58, "Relatório dashboard", size=14, color="#DDFBF1", bold=True)
    pdf.write_text(pdf.width - 198, 35, f"Mês analisado: {report['month']}", size=10, color="#FFFFFF")
    pdf.write_text(
        pdf.width - 198,
        54,
        f"Gerado em: {generated_at.strftime('%d/%m/%Y %H:%M')}",
        size=9,
        color="#DDE7F0",
    )
    pdf.y = 112

    add_pdf_section(pdf, "Resumo mensal", f"Score Trevo: {score['score']} - {score['label']}")
    add_pdf_summary_cards(
        pdf,
        [
            ("Salário", format_brl(dashboard.get("salaryBase") or 0), "#FFFFFF"),
            ("Entradas", format_brl(dashboard.get("inflow") or 0), "#E9F8EF"),
            ("Saídas", format_brl(dashboard.get("outflow") or 0), "#FDEEEF"),
            ("Saldo projetado", format_brl(dashboard.get("projectedBalance") or 0), "#EEF5FF"),
            ("Meta diária", format_brl(goals.get("dailyGoal") or goals.get("recommendedDailyGoal") or 0), "#F8F5FF"),
        ],
    )

    add_pdf_section(pdf, "Categorias", "Gastos por categoria com barras proporcionais.")
    category_pdf_rows = [
        {"name": row.get("name") or "Sem categoria", "total": row.get("total") or 0, "color": row.get("color") or "#14B8A6"}
        for row in category_rows
    ]
    add_pdf_bar_rows(pdf, category_pdf_rows, "name", "total", "Sem categorias para analisar.", "color")

    add_pdf_section(pdf, "Crescimento por categoria", f"Comparação com {growth.get('previousMonth') or 'o mês anterior'}.")
    if growth.get("hasHistory"):
        add_pdf_bar_rows(
            pdf,
            growth.get("items") or [],
            "name",
            "delta",
            "Ainda não há histórico suficiente para comparar.",
            "color",
        )
    else:
        pdf.write_text(pdf.margin, pdf.y, "Ainda não há histórico suficiente para comparar.", size=9, color="#6D7B8D")
        pdf.y += 22

    add_pdf_section(pdf, "Formas de pagamento", "Concentração de gastos por método de pagamento.")
    add_pdf_bar_rows(
        pdf,
        [{"payment_method": payment_method_label(row.get("payment_method")), "total": row.get("total") or 0} for row in payment_rows],
        "payment_method",
        "total",
        "Sem formas de pagamento para analisar.",
    )

    add_pdf_section(pdf, "Evolução", "Entradas, saídas e saldo dos últimos meses.")
    add_pdf_table(
        pdf,
        ["Mês", "Entradas", "Saídas", "Saldo"],
        [
            [
                row.get("label") or row.get("month") or "",
                format_brl(row.get("inflow") or 0),
                format_brl(row.get("outflow") or 0),
                format_brl(row.get("net") or 0),
            ]
            for row in trend_rows
        ],
        [84, 120, 120, 120],
        "Sem evolução mensal para analisar.",
    )

    add_pdf_section(pdf, "Alertas principais", "Pontos que merecem atenção no fechamento do mês.")
    alert_rows = report.get("alerts") or []
    if alert_rows:
        for alert in alert_rows[:5]:
            pdf.ensure_space(26)
            pdf.fill_rect(pdf.margin, pdf.y, pdf.width - pdf.margin * 2, 22, fill="#FFF7E7", stroke="#F2B84B")
            pdf.write_text(pdf.margin + 8, pdf.y + 14, truncate_pdf_text(alert.get("message") or "", 90), size=8, color="#102033")
            pdf.y += 28
    else:
        pdf.write_text(pdf.margin, pdf.y, "Nenhum alerta relevante para este mês.", size=9, color="#6D7B8D")
        pdf.y += 22

    add_pdf_section(pdf, "Movimentações", "Tabela das movimentações do mês analisado.")
    transaction_rows = []
    for row in rows:
        installment = ""
        if row.get("total_installments"):
            installment = f"{row.get('installment_number')}/{row.get('total_installments')}"
        transaction_rows.append(
            [
                str(row.get("transaction_date") or ""),
                "Entrada" if row.get("type") == "income" else "Despesa",
                str(row.get("title") or ""),
                str(row.get("category_name") or "Sem categoria"),
                payment_method_label(row.get("payment_method")),
                format_brl(row.get("amount") or 0),
                installment,
            ]
        )
    add_pdf_table(
        pdf,
        ["Data", "Tipo", "Nome", "Categoria", "Pagamento", "Valor", "Parcela"],
        transaction_rows,
        [52, 52, 130, 92, 76, 76, 46],
        "Nenhuma movimentação encontrada para este mês.",
    )
    return pdf.build()
