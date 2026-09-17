from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException

from app.budgets.service import find_matching_rule, list_categorization_rules
from app.categories.service import list_categories
from app.core.database import db_cursor, storage_available
from app.core.ephemeral import csv_import_sessions
from app.core.security import token_hash
from app.imports.parsers.dates import parse_import_datetime, parse_import_time, parse_import_type
from app.imports.schemas import CsvColumnMapping
from app.integrations.normalizer import build_duplicate_hash, normalize_duplicate_text, parse_decimal_text
from app.shared.dates import format_month_label, month_key_from_date
from app.shared.money import round_money
from app.shared.serialization import normalize_row, normalize_rows, require_row
from app.shared.validation import clean_text

CSV_IMPORT_PREVIEW_LIMIT = 10


def validate_csv_mapping(columns: list[str], mapping: CsvColumnMapping) -> None:
    required = [mapping.date, mapping.description, mapping.value]
    for optional in (mapping.type, mapping.category, mapping.account, mapping.time):
        if optional:
            required.append(optional)
    missing = [column for column in required if column not in columns]
    if missing:
        raise HTTPException(status_code=400, detail=f"Colunas não encontradas: {', '.join(missing)}.")



def get_csv_import_session(user_id: str, token: str) -> dict:
    if storage_available():
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT filename, columns_json, rows_json, created_at
                FROM csv_import_sessions_state
                WHERE token_hash = %s AND user_id = %s
                """,
                (token_hash(token), user_id),
            )
            row = normalize_row(cursor.fetchone())
        if row:
            columns_json = row["columns_json"]
            rows_json = row["rows_json"]
            columns = json.loads(columns_json) if isinstance(columns_json, str) else columns_json
            rows = json.loads(rows_json) if isinstance(rows_json, str) else rows_json
            return {
                "token": token,
                "user_id": user_id,
                "filename": row["filename"],
                "columns": columns,
                "rows": rows,
                "created_at": row["created_at"],
            }

    session = csv_import_sessions.get(token)
    if not session or session["user_id"] != user_id:
        raise HTTPException(status_code=404, detail="Importação não encontrada ou expirada.")
    return session



def cleanup_csv_import_sessions() -> None:
    cutoff = datetime.now(UTC) - timedelta(minutes=30)
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM csv_import_sessions_state WHERE created_at < %s", (cutoff,))
    for token, session in list(csv_import_sessions.items()):
        created_at = session.get("created_at")
        if not isinstance(created_at, datetime) or created_at < cutoff:
            csv_import_sessions.pop(token, None)



def resolve_import_category(
    categories: list[dict], rules: list[dict], raw_name, description: str
) -> tuple[int | None, str | None]:
    """Descobre a categoria de uma linha do extrato.

    Primeiro tenta o nome que veio no arquivo (casando por nome normalizado com
    as categorias do usuario); se nao houver coluna de categoria ou o nome nao
    casar, cai nas regras de categorizacao ja cadastradas.

    PERF-01: recebe categorias e regras já carregadas em vez de consultar o
    banco aqui dentro — chamada uma vez por LINHA do arquivo, isso fazia até
    duas idas ao banco por linha (list_categories + match_categorization_rule),
    até 10.000 no limite de 5.000 linhas.
    """
    name = str(raw_name or "").strip()
    if name:
        wanted = normalize_duplicate_text(name)
        for category in categories:
            if normalize_duplicate_text(str(category["name"])) == wanted:
                return int(category["id"]), str(category["name"])

    rule = find_matching_rule(rules, description)
    if rule:
        return int(rule["category_id"]), rule.get("category_name")
    return None, name or None



def build_csv_import_preview(user_id: str, session: dict, mapping: CsvColumnMapping) -> dict:
    validate_csv_mapping(session["columns"], mapping)

    # PERF-01: categorias e regras carregadas UMA VEZ por importação, não uma
    # vez por linha do arquivo — resolve_import_category fazia até duas idas
    # ao banco por linha, até 10.000 no limite de 5.000 linhas.
    categories = list_categories(user_id)
    rules = list_categorization_rules(user_id)

    parsed_rows: list[dict] = []
    errors_list: list[dict] = []
    duplicate_rows: list[dict] = []
    for index, row in enumerate(session["rows"], start=1):
        try:
            transaction_date, parsed_time = parse_import_datetime(row.get(mapping.date))
            if mapping.time:
                parsed_time = parse_import_time(row.get(mapping.time)) or parsed_time
            description = clean_text(row.get(mapping.description, ""), "Descrição", 200)
            signed_amount = parse_decimal_text(row.get(mapping.value))
            transaction_type = parse_import_type(row.get(mapping.type) if mapping.type else None, signed_amount)
            amount = round_money(abs(signed_amount))
            if amount <= 0:
                raise ValueError("Valor precisa ser maior que zero.")
            category_id, category_name = resolve_import_category(
                categories, rules, row.get(mapping.category) if mapping.category else None, description
            )
            account = (
                clean_text(row.get(mapping.account, ""), "Conta", 120, required=False) if mapping.account else None
            )
            year, month_number, day = (int(part) for part in transaction_date.split("-"))
            # FIN-01: o hash mais novo inclui hora e conta quando existem —
            # sem isso, duas transações legítimas e distintas no mesmo dia
            # (duas passagens de ônibus, mesma descrição e valor, hora
            # diferente) produziam o mesmo hash e uma era descartada como
            # duplicata. Os dois formatos antigos continuam sendo consultados
            # para não quebrar a deduplicação de lançamentos já importados
            # antes desta mudança.
            duplicate_hash = build_duplicate_hash(
                user_id, transaction_date, description, amount, transaction_type, time=parsed_time, account=account
            )
            type_aware_duplicate_hash = build_duplicate_hash(
                user_id, transaction_date, description, amount, transaction_type
            )
            legacy_duplicate_hash = build_duplicate_hash(user_id, transaction_date, description, amount)
            parsed_rows.append(
                {
                    "line": index,
                    "transactionDate": transaction_date,
                    "detectedMonth": month_key_from_date(transaction_date),
                    "monthLabel": format_month_label(month_key_from_date(transaction_date)),
                    "year": year,
                    "monthNumber": month_number,
                    "day": day,
                    "time": parsed_time,
                    "title": description,
                    "rawDescription": row.get(mapping.description, ""),
                    "amount": amount,
                    "type": transaction_type,
                    "categoryId": category_id,
                    "categoryName": category_name,
                    "account": account,
                    "duplicateHash": duplicate_hash,
                    "typeAwareDuplicateHash": type_aware_duplicate_hash,
                    "legacyDuplicateHash": legacy_duplicate_hash,
                }
            )
        except (ValueError, HTTPException) as exc:
            detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
            errors_list.append({"line": index, "detail": detail})

    existing_hashes: set[str] = set()
    if parsed_rows:
        duplicate_hashes = sorted(
            {
                candidate
                for row in parsed_rows
                for candidate in (
                    row["duplicateHash"],
                    row.get("typeAwareDuplicateHash"),
                    row.get("legacyDuplicateHash"),
                )
                if candidate
            }
        )
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT duplicate_hash
                FROM transactions
                WHERE user_id = %s AND duplicate_hash = ANY(%s)
                """,
                (user_id, duplicate_hashes),
            )
            existing_hashes = {row["duplicate_hash"] for row in normalize_rows(cursor.fetchall())}
        duplicate_rows = [
            row
            for row in parsed_rows
            if row["duplicateHash"] in existing_hashes
            or row.get("typeAwareDuplicateHash") in existing_hashes
            or row.get("legacyDuplicateHash") in existing_hashes
        ]

    # Ordem cronologica (e por hora, quando existe) para o preview refletir o
    # extrato de verdade, em vez da ordem crua do arquivo.
    parsed_rows.sort(key=lambda item: (item["transactionDate"], item["time"] or "", item["line"]))

    months = sorted({row["detectedMonth"] for row in parsed_rows})
    months_summary = [{"month": month, "label": format_month_label(month)} for month in months]
    existing_in_months = 0
    if months:
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS total
                FROM transactions
                WHERE user_id = %s
                  AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
                """,
                (user_id, months),
            )
            existing_in_months = int(
                require_row(normalize_row(cursor.fetchone()), "Totais nao encontrados.")["total"]
            )

    total_amount = round_money(sum((row["amount"] for row in parsed_rows), Decimal("0")))
    return {
        "importToken": session["token"],
        "columns": session["columns"],
        "totalRows": len(session["rows"]),
        "validRows": len(parsed_rows),
        "invalidRows": len(errors_list),
        "duplicateRows": len(duplicate_rows),
        "duplicates": duplicate_rows[:CSV_IMPORT_PREVIEW_LIMIT],
        "preview": parsed_rows[:CSV_IMPORT_PREVIEW_LIMIT],
        "errors": errors_list[:CSV_IMPORT_PREVIEW_LIMIT],
        "months": months_summary,
        "totalAmount": total_amount,
        # Quantos lancamentos ja existem nos meses do arquivo: e exatamente o
        # que o modo "substituir" apaga, e o modal precisa avisar antes.
        "existingInMonths": existing_in_months,
        "rows": parsed_rows,
    }

