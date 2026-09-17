from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder

import app.main as main_module
from app.api.deps import PlainDictRoute
from app.shared.dates import as_utc_datetime
from app.shared.serialization import normalize_row


def _flat_routes(app: FastAPI) -> list:
    """app.routes pode conter _IncludedRouter (roteamento preguiçoso do
    FastAPI moderno para routers incluídos via include_router) em vez da
    rota em si — original_router.routes tem a lista de verdade."""
    routes: list = []
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        routes.extend(original_router.routes if original_router is not None else [route])
    return routes


def test_decimal_serializa_como_numero():
    """Dinheiro precisa sair como número JSON, não string.

    O frontend declara esses campos como `number`; quando a serialização mudou
    para string, comparações e gráficos quebraram silenciosamente.
    """
    encoded = jsonable_encoder({"amount": Decimal("125.50"), "zero": Decimal("0.00")})
    assert encoded["amount"] == 125.5
    assert isinstance(encoded["amount"], float)
    assert encoded["zero"] == 0


def test_as_utc_datetime_aceita_iso_do_normalize_row():
    """normalize_row entrega datetime como texto ISO.

    as_utc_datetime precisa entender esse formato: era por não entender que a
    invalidação de token na troca de senha e o bloqueio de login por tentativas
    ficavam desligados sem erro nenhum.
    """
    moment = datetime.now(UTC).replace(microsecond=0)
    row = normalize_row({"password_changed_at": moment})
    assert isinstance(row["password_changed_at"], str)

    parsed = as_utc_datetime(row["password_changed_at"])
    assert parsed == moment

    # Um token emitido antes da troca precisa ser reconhecido como anterior.
    issued_before = moment - timedelta(seconds=5)
    assert issued_before < parsed


def test_as_utc_datetime_rejeita_lixo():
    assert as_utc_datetime(None) is None
    assert as_utc_datetime("nao é data") is None
    assert as_utc_datetime(12345) is None


def test_rota_anotada_devolve_dinheiro_como_numero():
    """O caso que realmente quebrou em produção.

    Anotar `-> dict` faz o FastAPI promover a anotação a response_model, e sob
    Pydantic v2 isso serializa Decimal como string. A route_class do app dispensa
    o response_model justamente para manter o contrato numérico — este teste
    monta uma rota com a mesma configuração do app real.
    """
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.router.route_class = PlainDictRoute

    @app.get("/valor")
    def valor() -> dict:
        return {"total": Decimal("2500.00")}

    @app.get("/lista")
    def lista() -> list[dict]:
        return [{"amount": Decimal("125.50")}]

    client = TestClient(app)
    body = client.get("/valor").json()
    assert body["total"] == 2500
    assert not isinstance(body["total"], str)

    linhas = client.get("/lista").json()
    assert linhas[0]["amount"] == 125.5
    assert not isinstance(linhas[0]["amount"], str)


def test_rotas_do_app_usam_a_route_class_sem_response_model():
    """Garante que nenhuma rota volte a herdar response_model da anotação."""
    monetarias = [r for r in _flat_routes(main_module.app) if getattr(r, "path", "").startswith("/api/")]
    assert monetarias, "nenhuma rota /api/ encontrada"
    assert all(getattr(r, "response_model", None) is None for r in monetarias)
