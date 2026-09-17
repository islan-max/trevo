from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from app.reports.service import payment_method_label
from app.shared.money import format_brl, round_money, to_decimal


def pdf_escape(value: Any) -> str:
    text = str(value or "").encode("latin-1", "replace").decode("latin-1")
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")



def pdf_color(hex_color: str) -> tuple[float, float, float]:
    cleaned = str(hex_color or "#102033").strip().lstrip("#")
    if len(cleaned) != 6:
        cleaned = "102033"
    try:
        red = int(cleaned[0:2], 16) / 255
        green = int(cleaned[2:4], 16) / 255
        blue = int(cleaned[4:6], 16) / 255
    except ValueError:
        red, green, blue = pdf_color("#102033")
    return red, green, blue



def pdf_color_command(hex_color: str, mode: str) -> str:
    red, green, blue = pdf_color(hex_color)
    return f"{red:.3f} {green:.3f} {blue:.3f} {mode}"



class PdfReport:
    width = 595
    height = 842
    margin = 36

    def __init__(self) -> None:
        self.pages: list[list[str]] = [[]]
        self.y = self.margin

    @property
    def commands(self) -> list[str]:
        return self.pages[-1]

    def add_page(self) -> None:
        self.pages.append([])
        self.y = self.margin

    def ensure_space(self, height: float) -> None:
        if self.y + height > self.height - 58:
            self.add_page()

    def rect(self, x: float, y: float, width: float, height: float, fill: str = "#FFFFFF", stroke: str | None = None) -> None:
        y_pdf = self.height - y - height
        operator = "B" if stroke else "f"
        if fill:
            self.commands.append(pdf_color_command(fill, "rg"))
        if stroke:
            self.commands.append(pdf_color_command(stroke, "RG"))
        self.commands.append(f"{x:.1f} {y_pdf:.1f} {width:.1f} {height:.1f} re {operator}")

    def line(self, x1: float, y1: float, x2: float, y2: float, color: str = "#DDE7F0", width: float = 1) -> None:
        self.commands.append(pdf_color_command(color, "RG"))
        self.commands.append(f"{width:.1f} w {x1:.1f} {self.height - y1:.1f} m {x2:.1f} {self.height - y2:.1f} l S")

    def text(self, x: float, y: float, text: Any, size: int = 10, color: str = "#102033", bold: bool = False) -> None:
        font = "F2" if bold else "F1"
        self.commands.append(pdf_color_command(color, "rg"))
        self.commands.append(f"BT /{font} {size} Tf {x:.1f} {self.height - y:.1f} Td ({pdf_escape(text)}) Tj ET")

    def wrapped_text(self, x: float, y: float, text: str, max_chars: int, size: int = 9, color: str = "#102033") -> float:
        lines = wrap_pdf_line(text, max_chars)
        for index, line in enumerate(lines):
            self.text(x, y + index * (size + 3), line, size=size, color=color)
        return y + len(lines) * (size + 3)

    def build(self) -> bytes:
        for index, commands in enumerate(self.pages, start=1):
            commands.append(pdf_color_command("#DDE7F0", "RG"))
            commands.append(
                f"1.0 w {self.margin:.1f} 42.0 m {self.width - self.margin:.1f} 42.0 l S"
            )
            commands.append(pdf_color_command("#6D7B8D", "rg"))
            commands.append(f"BT /F2 9 Tf {self.margin:.1f} 24.0 Td (Trevo) Tj ET")
            commands.append(
                f"BT /F1 9 Tf {self.width - 96:.1f} 24.0 Td ({pdf_escape(f'Página {index}')}) Tj ET"
            )

        page_count = len(self.pages)
        font_regular_id = 3 + page_count * 2
        font_bold_id = font_regular_id + 1
        objects: dict[int, bytes] = {
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            font_regular_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            font_bold_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
        }

        page_ids: list[int] = []
        for index, commands in enumerate(self.pages):
            page_id = 3 + index * 2
            content_id = page_id + 1
            page_ids.append(page_id)
            stream = "\n".join(commands).encode("latin-1", "replace")
            objects[content_id] = (
                f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
                + stream
                + b"\nendstream"
            )
            objects[page_id] = (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.width} {self.height}] "
                f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            ).encode("ascii")

        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
        objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii")

        pdf = b"%PDF-1.4\n"
        offsets = [0]
        for object_id in range(1, max(objects) + 1):
            offsets.append(len(pdf))
            pdf += f"{object_id} 0 obj\n".encode("ascii") + objects[object_id] + b"\nendobj\n"

        xref_offset = len(pdf)
        pdf += f"xref\n0 {len(offsets)}\n".encode("ascii")
        pdf += b"0000000000 65535 f \n"
        for offset in offsets[1:]:
            pdf += f"{offset:010d} 00000 n \n".encode("ascii")
        pdf += (
            f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
        return pdf



def wrap_pdf_line(line: str, max_length: int = 92) -> list[str]:
    words = line.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + len(word) + 1 <= max_length:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines



def build_basic_pdf(lines: list[str]) -> bytes:
    prepared_lines: list[str] = []
    for line in lines:
        prepared_lines.extend(wrap_pdf_line(line))

    max_lines_per_page = 46
    pages = [
        prepared_lines[index : index + max_lines_per_page]
        for index in range(0, len(prepared_lines), max_lines_per_page)
    ] or [["Sem dados para exibir."]]

    page_count = len(pages)
    font_object_id = 3 + page_count * 2
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        font_object_id: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }

    page_ids: list[int] = []
    for index, page_lines in enumerate(pages):
        page_id = 3 + index * 2
        content_id = page_id + 1
        page_ids.append(page_id)

        commands = ["BT", "/F1 11 Tf", "50 800 Td"]
        for line_index, line in enumerate(page_lines):
            if line_index:
                commands.append("0 -16 Td")
            commands.append(f"({pdf_escape(line)}) Tj")
        commands.append("ET")

        stream = "\n".join(commands).encode("latin-1", "replace")
        objects[content_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_object_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>".encode("ascii")

    pdf = b"%PDF-1.4\n"
    offsets = [0]
    for object_id in range(1, max(objects) + 1):
        offsets.append(len(pdf))
        pdf += f"{object_id} 0 obj\n".encode("ascii") + objects[object_id] + b"\nendobj\n"

    xref_offset = len(pdf)
    pdf += f"xref\n0 {len(offsets)}\n".encode("ascii")
    pdf += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        pdf += f"{offset:010d} 00000 n \n".encode("ascii")
    pdf += (
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode("ascii")
    return pdf



def truncate_pdf_text(value: Any, max_length: int) -> str:
    text = str(value or "")
    return text if len(text) <= max_length else text[: max_length - 1] + "…"



def add_pdf_section(pdf: PdfReport, title: str, description: str | None = None) -> None:
    pdf.ensure_space(42)
    pdf.text(pdf.margin, pdf.y, title, size=14, color="#102033", bold=True)
    pdf.y += 16
    if description:
        pdf.text(pdf.margin, pdf.y, description, size=9, color="#6D7B8D")
        pdf.y += 14
    pdf.line(pdf.margin, pdf.y, pdf.width - pdf.margin, pdf.y, "#DDE7F0")
    pdf.y += 14



def add_pdf_summary_cards(pdf: PdfReport, cards: list[tuple[str, str, str]]) -> None:
    card_gap = 8
    card_width = (pdf.width - pdf.margin * 2 - card_gap * 4) / 5
    card_height = 58
    pdf.ensure_space(card_height + 12)
    for index, (label, value, tone) in enumerate(cards):
        x = pdf.margin + index * (card_width + card_gap)
        pdf.rect(x, pdf.y, card_width, card_height, fill=tone, stroke="#DDE7F0")
        pdf.text(x + 8, pdf.y + 16, label, size=7, color="#6D7B8D", bold=True)
        pdf.text(x + 8, pdf.y + 38, value, size=10, color="#102033", bold=True)
    pdf.y += card_height + 16



def add_pdf_bar_rows(
    pdf: PdfReport,
    rows: list[dict],
    label_key: str,
    value_key: str,
    empty_text: str,
    color_key: str | None = None,
    limit: int = 8,
) -> None:
    if not rows:
        pdf.ensure_space(22)
        pdf.text(pdf.margin, pdf.y, empty_text, size=9, color="#6D7B8D")
        pdf.y += 20
        return

    visible_rows = rows[:limit]
    max_value = max([abs(to_decimal(row.get(value_key) or 0)) for row in visible_rows] + [Decimal("1")])
    for row in visible_rows:
        pdf.ensure_space(31)
        value = round_money(row.get(value_key) or 0)
        label = truncate_pdf_text(row.get(label_key) or "Sem categoria", 30)
        pdf.text(pdf.margin, pdf.y + 8, label, size=9, color="#102033", bold=True)
        pdf.text(pdf.width - pdf.margin - 96, pdf.y + 8, format_brl(value), size=9, color="#102033")
        bar_x = pdf.margin
        bar_y = pdf.y + 15
        bar_width = pdf.width - pdf.margin * 2
        pdf.rect(bar_x, bar_y, bar_width, 8, fill="#EEF5F8")
        filled_width = float(abs(value) / max_value) * bar_width if max_value > 0 else 0
        fill_color = row.get(color_key or "") or ("#E14B5A" if value > 0 and value_key == "delta" else "#14B8A6")
        if value_key == "delta" and value < 0:
            fill_color = "#18A957"
        pdf.rect(bar_x, bar_y, max(2, filled_width), 8, fill=fill_color)
        pdf.y += 31



def add_pdf_table(pdf: PdfReport, headers: list[str], rows: list[list[str]], widths: list[float], empty_text: str) -> None:
    if not rows:
        pdf.ensure_space(22)
        pdf.text(pdf.margin, pdf.y, empty_text, size=9, color="#6D7B8D")
        pdf.y += 20
        return

    def draw_header() -> None:
        pdf.rect(pdf.margin, pdf.y, sum(widths), 22, fill="#EEF5F8", stroke="#DDE7F0")
        x = pdf.margin + 6
        for index, header in enumerate(headers):
            pdf.text(x, pdf.y + 14, header, size=8, color="#102033", bold=True)
            x += widths[index]
        pdf.y += 22

    draw_header()
    for row in rows:
        pdf.ensure_space(24)
        if pdf.y < 60:
            draw_header()
        pdf.line(pdf.margin, pdf.y, pdf.margin + sum(widths), pdf.y, "#DDE7F0")
        x = pdf.margin + 6
        for index, cell in enumerate(row):
            pdf.text(x, pdf.y + 15, truncate_pdf_text(cell, max(10, int(widths[index] / 4.8))), size=8, color="#102033")
            x += widths[index]
        pdf.y += 24
    pdf.y += 8



def build_report_pdf(report: dict, rows: list[dict], generated_at: datetime) -> bytes:
    pdf = PdfReport()
    dashboard = report["dashboard"]
    goals = report["goals"]
    score = report["score"]
    category_rows = dashboard.get("categoryBreakdown") or []
    payment_rows = dashboard.get("paymentMethodBreakdown") or []
    trend_rows = (dashboard.get("monthlyTrend") or [])[-6:]
    growth = report.get("categoryGrowth") or {"hasHistory": False, "items": []}

    pdf.rect(0, 0, pdf.width, 92, fill="#0A1728")
    pdf.text(pdf.margin, 34, "Trevo", size=23, color="#FFFFFF", bold=True)
    pdf.text(pdf.margin, 58, "Relatório dashboard", size=14, color="#DDFBF1", bold=True)
    pdf.text(pdf.width - 198, 35, f"Mês analisado: {report['month']}", size=10, color="#FFFFFF")
    pdf.text(
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
        pdf.text(pdf.margin, pdf.y, "Ainda não há histórico suficiente para comparar.", size=9, color="#6D7B8D")
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
            pdf.rect(pdf.margin, pdf.y, pdf.width - pdf.margin * 2, 22, fill="#FFF7E7", stroke="#F2B84B")
            pdf.text(pdf.margin + 8, pdf.y + 14, truncate_pdf_text(alert.get("message") or "", 90), size=8, color="#102033")
            pdf.y += 28
    else:
        pdf.text(pdf.margin, pdf.y, "Nenhum alerta relevante para este mês.", size=9, color="#6D7B8D")
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

