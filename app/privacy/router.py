from __future__ import annotations

import json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder

from app.api.deps import (
    AUTH_COOKIE_NAME,
    PlainDictRoute,
    clear_auth_cookie,
    enforce_ip_rate_limit,
    get_current_user,
    limiter,
    oauth2_scheme,
    revoke_token,
)
from app.core.database import db_cursor
from app.core.logging import audit_log, client_ip_hash
from app.privacy.schemas import ConsentPayload
from app.privacy.service import POLICY_VERSION, build_data_export, record_consent

router = APIRouter(route_class=PlainDictRoute)


@router.get("/api/privacy/export")
@limiter.limit("10 per 1 hour")
def export_my_data(request: Request, current_user: dict = Depends(get_current_user)) -> Response:
    """LGPD Art. 18 — acesso e portabilidade: todos os dados do titular em JSON."""
    enforce_ip_rate_limit(request, "privacy_export", max_attempts=10, window_seconds=3600)
    with db_cursor() as cursor:
        data = build_data_export(cursor, current_user["id"])
    body = json.dumps(jsonable_encoder(data), ensure_ascii=False, indent=2)
    audit_log("data_exported", current_user["id"])
    return Response(
        content=body,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="trevo-meus-dados.json"'},
    )



@router.post("/api/privacy/consent")
def update_consent(
    request: Request,
    response: Response,
    payload: ConsentPayload,
    token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Registra concessão/revogação de consentimento (Arts. 7/8).

    Para recursos opcionais (ex.: resumo mensal), mantém o flag do usuário em
    sincronia com o ledger de consentimento.
    """
    allowed_scopes = {"monthly_summary", "terms_privacy"}
    if payload.scope not in allowed_scopes:
        raise HTTPException(status_code=400, detail="Escopo de consentimento inválido.")

    deactivated = False
    ip_hash = client_ip_hash(request)
    with db_cursor(commit=True) as cursor:
        record_consent(
            cursor,
            current_user["id"],
            scope=payload.scope,
            granted=payload.granted,
            ip_hash=ip_hash,
            channel="settings",
        )
        if payload.scope == "monthly_summary":
            cursor.execute(
                "UPDATE users SET send_monthly_summary = %s WHERE id = %s",
                (payload.granted, current_user["id"]),
            )
        elif payload.scope == "terms_privacy" and not payload.granted:
            # SEC-09: revogar o consentimento que sustenta o próprio serviço
            # não pode ser um no-op — antes, a conta continuava plenamente
            # ativa depois de "revogada". Desativa a conta (reversível,
            # diferente de excluir) em vez de apagar dados sem a confirmação
            # por senha que DELETE /api/auth/me exige.
            cursor.execute("UPDATE users SET is_active = FALSE WHERE id = %s", (current_user["id"],))
            deactivated = True

    if deactivated:
        active_token = token or cookie_token
        if active_token:
            revoke_token(active_token, current_user["id"])
        clear_auth_cookie(response)
        audit_log("account_deactivated_consent_revoked", current_user["id"])

    return {
        "scope": payload.scope,
        "granted": payload.granted,
        "policy_version": POLICY_VERSION,
        "accountDeactivated": deactivated,
    }

