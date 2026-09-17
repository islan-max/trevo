from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from psycopg2.extras import Json, execute_values

from app.api.deps import PlainDictRoute, enforce_ip_rate_limit, get_current_user
from app.core.database import db_cursor, storage_available
from app.core.ephemeral import csv_import_sessions
from app.core.logging import audit_log
from app.core.security import token_hash
from app.imports.parsers.csv import parse_csv_rows
from app.imports.schemas import CsvImportConfirmPayload, CsvImportPreviewPayload
from app.imports.service import CSV_IMPORT_PREVIEW_LIMIT, build_csv_import_preview, cleanup_csv_import_sessions, get_csv_import_session
from app.shared.serialization import normalize_rows

CSV_IMPORT_MAX_BYTES = 1024 * 1024
CSV_IMPORT_ALLOWED_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
}

router = APIRouter(route_class=PlainDictRoute)


@router.post("/api/imports/csv/upload")
def upload_csv_import(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    # CSV-10: sem limite, um upload grava até 1 MB de JSONB por chamada em
    # csv_import_sessions_state.
    enforce_ip_rate_limit(request, "csv_upload", max_attempts=20, window_seconds=3600)
    cleanup_csv_import_sessions()
    filename = file.filename or ""
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Envie um arquivo com extensão .csv.")
    if content_type not in CSV_IMPORT_ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="Content-Type de CSV inválido.")

    content = file.file.read(CSV_IMPORT_MAX_BYTES + 1)
    if len(content) > CSV_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="CSV excede o tamanho máximo de 1 MB.")

    columns, rows = parse_csv_rows(content)
    token = secrets.token_urlsafe(32)
    csv_import_sessions[token] = {
        "token": token,
        "user_id": current_user["id"],
        "filename": filename,
        "columns": columns,
        "rows": rows,
        "created_at": datetime.now(UTC),
    }
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO csv_import_sessions_state
                  (token_hash, user_id, filename, columns_json, rows_json, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                """,
                (token_hash(token), current_user["id"], filename, Json(columns), Json(rows)),
            )
    return {
        "importToken": token,
        "filename": filename,
        "columns": columns,
        "totalRows": len(rows),
        "preview": rows[:CSV_IMPORT_PREVIEW_LIMIT],
    }



@router.post("/api/imports/csv/preview")
def preview_csv_import(payload: CsvImportPreviewPayload, current_user: dict = Depends(get_current_user)) -> dict:
    session = get_csv_import_session(current_user["id"], payload.importToken)
    preview = build_csv_import_preview(current_user["id"], session, payload.mapping)
    preview.pop("rows", None)
    return preview



@router.post("/api/imports/csv/confirm")
def confirm_csv_import(payload: CsvImportConfirmPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    session = get_csv_import_session(user_id, payload.importToken)
    preview = build_csv_import_preview(user_id, session, payload.mapping)

    replaced = 0
    months = [entry["month"] for entry in preview["months"]]
    # Identifica este lote — usado para rastrear quais lançamentos vieram
    # desta importação específica (relatórios futuros, "desfazer importação").
    import_batch_id = str(uuid.uuid4())

    with db_cursor(commit=True) as cursor:
        if payload.mode == "replace" and months:
            # Substituir troca os lançamentos IMPORTADOS POR CSV dos meses
            # presentes no arquivo — e só deles. Meses fora do CSV ficam
            # intactos, e lançamentos manuais ou parcelas de cartão nunca são
            # tocados: sem o filtro por source, "substituir" apagava o
            # aluguel digitado à mão e mutilava grupos de parcelamento pela
            # metade (DATA-01). installment_group IS NULL é redundante hoje
            # — a importação nunca grava parcelas — e fica como cinto e
            # suspensório contra uma mudança futura.
            cursor.execute(
                """
                DELETE FROM transactions
                WHERE user_id = %s
                  AND source = 'csv_import'
                  AND installment_group IS NULL
                  AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
                """,
                (user_id, months),
            )
            replaced = cursor.rowcount or 0
            audit_log("csv_import_replace", user_id, {"months": months, "deleted": replaced})

        # Duplicatas contra o estado ATUAL do banco — pós-delete, se "replace"
        # rodou acima. Uma única consulta em lote (não uma por linha —
        # PERF-01), recalculada aqui (não reaproveitada do preview) porque em
        # modo "replace" o preview foi calculado ANTES do DELETE: usar o
        # resultado dele aqui trataria como "duplicata" uma linha cujo
        # correspondente acabou de ser apagado.
        all_candidate_hashes = sorted(
            {
                candidate
                for row in preview["rows"]
                for candidate in (
                    row["duplicateHash"],
                    row.get("typeAwareDuplicateHash"),
                    row.get("legacyDuplicateHash"),
                )
                if candidate
            }
        )
        existing_hashes: set[str] = set()
        if all_candidate_hashes:
            cursor.execute(
                "SELECT duplicate_hash FROM transactions WHERE user_id = %s AND duplicate_hash = ANY(%s)",
                (user_id, all_candidate_hashes),
            )
            existing_hashes = {row["duplicate_hash"] for row in normalize_rows(cursor.fetchall())}

        candidate_rows = []
        duplicates: list[dict] = []
        for row in preview["rows"]:
            if (
                row["duplicateHash"] in existing_hashes
                or row.get("typeAwareDuplicateHash") in existing_hashes
                or row.get("legacyDuplicateHash") in existing_hashes
            ):
                duplicates.append(row)
            else:
                candidate_rows.append(row)

        # PERF-01: era um SELECT de duplicata + um INSERT por linha (até
        # 5.000 idas ao banco cada, em serverless cada uma abrindo conexão
        # própria). ON CONFLICT DO NOTHING fica como cinto e suspensório
        # contra duas linhas idênticas dentro do próprio arquivo (Postgres
        # descarta conflitos dentro do mesmo INSERT também), usando o índice
        # único que já existe. RETURNING * devolve só as linhas que entraram
        # de fato.
        #
        # CSV-08/09: payment_method deixa de receber a conta (que polui o
        # paymentMethodBreakdown do dashboard com nomes de conta em vez de
        # forma de pagamento) — vai para a coluna account, dedicada.
        # external_id deixa de receber o duplicate_hash (era um bug: os dois
        # campos têm propósitos diferentes).
        values = [
            (
                user_id,
                row["title"],
                row["amount"],
                row["type"],
                row.get("categoryId"),
                "csv_import",
                row["transactionDate"],
                f"Importado às {row['time']}" if row.get("time") else "",
                row["detectedMonth"],
                row["duplicateHash"],
                row["rawDescription"],
                import_batch_id,
                row.get("account"),
            )
            for row in candidate_rows
        ]

        imported: list[dict] = []
        if values:
            inserted_rows = execute_values(
                cursor,
                """
                INSERT INTO transactions
                  (user_id, title, amount, type, category_id, payment_method, transaction_date, notes,
                   billing_month, duplicate_hash, raw_description, import_batch_id, account,
                   card_id, installment_group, installment_number, total_installments, source, external_id,
                   imported_at)
                VALUES %s
                ON CONFLICT (user_id, duplicate_hash) WHERE duplicate_hash IS NOT NULL DO NOTHING
                RETURNING *
                """,
                values,
                template=(
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,"
                    " NULL, NULL, NULL, NULL, 'csv_import', NULL, NOW())"
                ),
                fetch=True,
            )
            imported = normalize_rows(inserted_rows)

        # Alguma linha ainda pode ter sido descartada pelo ON CONFLICT (duas
        # linhas idênticas dentro do próprio arquivo) — conta como duplicata
        # também.
        inserted_hashes = {row["duplicate_hash"] for row in imported}
        duplicates.extend(row for row in candidate_rows if row["duplicateHash"] not in inserted_hashes)

    csv_import_sessions.pop(payload.importToken, None)
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM csv_import_sessions_state WHERE token_hash = %s AND user_id = %s",
                (token_hash(payload.importToken), user_id),
            )
    audit_log(
        "csv_import_confirmed",
        user_id,
        {"mode": payload.mode, "imported": len(imported), "duplicates": len(duplicates), "replaced": replaced},
    )
    return {
        "imported": len(imported),
        "duplicates": len(duplicates),
        "invalidRows": preview["invalidRows"],
        "replaced": replaced,
        "mode": payload.mode,
        "months": preview["months"],
        "transactions": imported,
    }

