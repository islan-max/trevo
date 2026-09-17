
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from dotenv import load_dotenv
from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.routing import APIRoute
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from jose import JWTError, jwt
from psycopg2 import errors
from psycopg2.extras import Json, execute_values
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.concurrency import run_in_threadpool
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import Response

from app.core import storage
from app.core.config import settings
from app.core.database import (
    close_db_pool,
    close_request_scope,
    connection,
    db_cursor,
    get_database_url,
    init_db_pool,
    open_request_scope,
    storage_available,
)
from app.core.logging import JsonLogFormatter
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    hash_password,
    hash_pin,
    token_hash,
    validate_password_strength,
    validate_pin,
    verify_password,
    verify_pin,
)
from app.core.signing import resolve_jwt_secret, secret_source
from app.integrations.normalizer import (
    build_duplicate_hash,
    normalize_duplicate_text,
    parse_decimal_text,
)
from app.oauth import (
    OAUTH_PROVIDERS,
    OAUTH_STATE_COOKIE,
    build_authorize_redirect,
    fetch_oauth_profile,
    frontend_redirect,
    list_providers,
    set_request_origin,
)
from app.privacy.service import (
    POLICY_VERSION,
    build_data_export,
    record_consent,
)
from app.shared import clock
from app.shared.dates import (
    add_months,
    first_billing_month,
    format_month_label,
    get_current_month,
    month_key_from_date,
)
from app.shared.money import (
    apply_installment_interest,
    distribute_installments,
    format_brl,
    round_money,
    to_decimal,
)
from migrate import run_migrations, run_migrations_locked

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_OUT_DIR = BASE_DIR / "frontend" / "out"

JWT_ALGORITHM = settings.jwt_algorithm
ACCESS_TOKEN_EXPIRE_HOURS = settings.access_token_expire_hours
AUTH_COOKIE_NAME = "trevo_access_token"
CSRF_COOKIE_NAME = "trevo_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
# State-changing endpoints reached before a session cookie exists (so no CSRF risk).
CSRF_EXEMPT_PATHS = {"/api/auth/login", "/api/auth/register", "/api/auth/csrf"}
CSRF_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CARD_UNLOCK_SECONDS = 15 * 60
PIN_FAILURE_WINDOW_SECONDS = 5 * 60
PIN_MAX_ATTEMPTS = 3
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
CSV_IMPORT_MAX_BYTES = 1024 * 1024
CSV_IMPORT_MAX_ROWS = 5000
CSV_IMPORT_PREVIEW_LIMIT = 10
CSV_IMPORT_ALLOWED_CONTENT_TYPES = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
}
PROFILE_PHOTO_DIR = BASE_DIR / "data" / "profile-photos"
PROFILE_PHOTO_URL_PREFIX = "/media/profile-photos"
PROFILE_PHOTO_MAX_BYTES = 512 * 1024
PROFILE_PHOTO_ALLOWED_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}
LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "text")


if LOG_FORMAT == "json":
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO), handlers=[handler], force=True)
else:
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
logger = logging.getLogger("trevo")


def email_hash(email: str) -> str:
    return hashlib.sha256(email.encode()).hexdigest()[:16]


def client_ip_hash(request: Request) -> str | None:
    ip = request.client.host if request.client else ""
    if not ip:
        return None
    return hashlib.sha256(ip.encode()).hexdigest()[:16]


def audit_log(event: str, user_id: str | None, details: dict | None = None) -> None:
    entry = {
        "audit": True,
        "event": event,
        "user_id": user_id,
        "timestamp": datetime.now(UTC).isoformat(),
        **(details or {}),
    }
    logger.info(json.dumps(entry))


# Dinheiro sai da API como número JSON, nunca string.
#
# As rotas anotam `-> dict` / `-> list[dict]`, e o FastAPI promove a anotação de
# retorno a response_model. Sob Pydantic v2 isso muda o serializador: Decimal
# passa a virar string ("2500.00" em vez de 2500). O frontend declara esses
# campos como number, então todo valor monetário chegava quebrado.
#
# As anotações são genéricas (dict), ou seja, não validam nada de útil — a
# resposta é montada à mão. Dispensar o response_model devolve o encoder padrão
# do FastAPI, que serializa Decimal como float.
class PlainDictRoute(APIRoute):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["response_model"] = None
        super().__init__(*args, **kwargs)


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)
limiter = Limiter(key_func=get_remote_address)
card_pin_failures: dict[str, dict[str, Any]] = {}
card_unlock_sessions: dict[str, dict[str, Any]] = {}
csv_import_sessions: dict[str, dict[str, Any]] = {}
login_failures: dict[str, dict[str, Any]] = {}
revoked_token_hashes: set[str] = set()
startup_time = time.time()

DEFAULT_CATEGORIES: list[tuple[str, str, str, str, int]] = [
    ("Sal\u00e1rio", "income", "#2E9D5B", "\U0001f4bc", 1),
    ("Freelance", "income", "#4FB877", "\U0001f9e0", 1),
    ("Investimentos", "income", "#7FD199", "\U0001f4c8", 1),
    ("Moradia", "expense", "#D9A441", "\U0001f3e0", 1),
    ("Alimenta\u00e7\u00e3o", "expense", "#E4884A", "\U0001f37d\ufe0f", 1),
    ("Mercado", "expense", "#C97B9E", "\U0001f6d2", 1),
    ("Transporte", "expense", "#4E8FBF", "\U0001f68c", 1),
    ("Sa\u00fade", "expense", "#D1495B", "\U0001f48a", 1),
    ("Educa\u00e7\u00e3o", "expense", "#8B7BC4", "\U0001f4da", 1),
    ("Assinaturas", "expense", "#4CA9A0", "\U0001f4fa", 1),
    ("Lazer", "expense", "#E0658A", "\U0001f3ae", 1),
    ("Contas", "expense", "#7A8B99", "\U0001f4a1", 1),
    ("Reserva", "expense", "#1F8049", "\U0001f4b0", 1),
    ("Pets", "expense", "#B08968", "\U0001f436", 1),
    ("Presentes", "expense", "#E07A5F", "\U0001f381", 1),
    ("Outros", "expense", "#96A5A0", "\U0001f4cc", 1),
]


def is_production() -> bool:
    return settings.is_production


ALLOWED_ORIGINS = settings.allowed_origins
if not settings.is_serverless:
    PROFILE_PHOTO_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ciclo de vida do processo.

    Substitui os antigos `@app.on_event`, removidos nas versões novas do
    Starlette — era por isso que a suíte quebrava com
    `'APIRouter' object has no attribute 'startup'`.
    """
    global startup_time
    startup_time = time.time()
    logger.info("Starting Trevo")
    # Serverless (Vercel) valida config e conecta por request; validar, abrir
    # pool e migrar no cold start derrubaria a função inteira se faltasse env.
    # Lá as migrations rodam via ensure_serverless_schema, no primeiro request.
    if not settings.is_serverless:
        # O pool e as migrations vêm antes da validação porque o segredo de
        # assinatura pode ser provisionado na tabela app_secrets quando
        # JWT_SECRET_KEY não está no ambiente.
        get_database_url()
        init_db_pool()
        with connection() as conn:
            run_migrations(conn)
        validate_runtime_config()
        logger.info("Startup completed")
    else:
        logger.info("Serverless mode: skipping startup validation, pool and migrations")
    try:
        yield
    finally:
        logger.info("Shutting down Trevo")
        close_db_pool()


app = FastAPI(title="Trevo API", version="2.0.0", lifespan=lifespan)
app.router.route_class = PlainDictRoute
app.state.limiter = limiter
if not settings.is_serverless:
    app.mount(PROFILE_PHOTO_URL_PREFIX, StaticFiles(directory=PROFILE_PHOTO_DIR), name="profile-photos")


def rate_limit_handler(request: Request, exc: Exception) -> Response:
    if isinstance(exc, RateLimitExceeded):
        return _rate_limit_exceeded_handler(request, exc)
    raise exc


app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials="*" not in ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "X-Card-Unlock-Token", "X-Token-Response"],
)
# SEC-06: app/oauth.py deriva o redirect do fluxo OAuth de request.base_url,
# que vem do header Host sem validação — um Host forjado produz um redirect
# para domínio arbitrário depois do login. Só ativa quando TRUSTED_HOSTS está
# configurado (ver Settings.trusted_hosts); sem isso, a ausência de allowlist
# já é sinalizada em validate_runtime_config().
if settings.trusted_hosts:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)


# Agregados caros (orçamento, metas) são pedidos várias vezes dentro da mesma
# request — /api/bootstrap sozinho pedia o orçamento 3x e as metas 2x. O cache
# vive só enquanto a request dura, então nunca serve dado velho entre requests.
_request_cache: ContextVar[dict | None] = ContextVar("request_cache", default=None)


def request_cached(key: tuple, factory):
    cache = _request_cache.get()
    if cache is None:
        return factory()
    if key not in cache:
        cache[key] = factory()
    return cache[key]


# Em serverless o startup não roda migrations (um banco fora do ar derrubaria o
# cold start inteiro) e o build da Vercel também não as roda — o schema ficava
# congelado no que existisse. Aqui elas rodam uma vez por processo, sob advisory
# lock e sem poder derrubar a request se falharem.
_schema_checked = False


def ensure_serverless_schema() -> None:
    global _schema_checked
    if _schema_checked or not settings.is_serverless:
        return
    _schema_checked = True  # uma tentativa por processo, mesmo se falhar
    try:
        with connection() as conn:
            run_migrations_locked(conn)
        logger.info("Serverless schema check completed")
    except Exception:
        logger.exception("Serverless migration check failed; serving anyway")


@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = str(uuid.uuid4())[:8]
    request.state.request_id = request_id
    _request_cache.set({})
    if not _schema_checked and request.url.path.startswith("/api/"):
        # Roda antes de open_request_scope(): essa checagem usa sua própria
        # conexão de uso único (run_in_threadpool copia o contexto atual para
        # a thread, então uma conexão cacheada lá dentro nunca seria fechada
        # de volta no contexto da request).
        await run_in_threadpool(ensure_serverless_schema)
    open_request_scope()
    try:
        response = await call_next(request)
        response.headers["X-Request-Id"] = request_id
        return response
    finally:
        close_request_scope()


@app.middleware("http")
async def validate_content_type(request: Request, call_next):
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if (
        request.method in ("POST", "PUT", "PATCH")
        and request.url.path.startswith("/api/")
        and request.url.path != "/api/auth/login"
        and content_type not in ("application/json", "multipart/form-data", "application/x-www-form-urlencoded", "")
    ):
        return JSONResponse(
            {"detail": "Content-Type inválido."},
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        )
    return await call_next(request)


@app.middleware("http")
async def csrf_protect(request: Request, call_next):
    # Double-submit CSRF: for state-changing /api requests authenticated by the
    # session cookie (not a Bearer token), require the X-CSRF-Token header to
    # match the trevo_csrf cookie. Bearer/API clients are exempt, and requests
    # before login (no auth cookie yet) are naturally exempt.
    path = request.url.path
    if (
        request.method not in CSRF_SAFE_METHODS
        and path.startswith("/api/")
        and path not in CSRF_EXEMPT_PATHS
        and not path.startswith("/api/auth/oauth/")
    ):
        has_bearer = request.headers.get("authorization", "").lower().startswith("bearer ")
        auth_cookie = request.cookies.get(AUTH_COOKIE_NAME)
        if auth_cookie and not has_bearer:
            cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)
            header_csrf = request.headers.get(CSRF_HEADER_NAME)
            if not cookie_csrf or not header_csrf or not secrets.compare_digest(cookie_csrf, header_csrf):
                return JSONResponse({"detail": "CSRF token inválido ou ausente."}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    started_at = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "Request failed method=%s path=%s",
            request.method,
            request.url.path,
            extra={"request_id": getattr(request.state, "request_id", None)},
        )
        raise

    elapsed_ms = round((time.perf_counter() - started_at) * 1000, 2)
    logger.info(
        "Request completed method=%s path=%s status=%s duration_ms=%s",
        request.method,
        request.url.path,
        response.status_code,
        elapsed_ms,
        extra={"request_id": getattr(request.state, "request_id", None)},
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = (
        "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
    if is_production():
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    # 'unsafe-inline' on script-src is required by Next.js static export (inline
    # hydration bootstrap, no nonce support in export mode). External script/font
    # origins are dropped since the frontend self-hosts everything.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "upgrade-insecure-requests"
    )
    return response


def get_jwt_secret() -> str:
    return resolve_jwt_secret()


def validate_runtime_config() -> None:
    get_database_url()
    get_jwt_secret()
    if is_production():
        origins = ALLOWED_ORIGINS
        if not origins:
            logger.info("ALLOWED_ORIGINS is empty in production. Cross-origin browser requests are disabled.")
        if any("localhost" in origin or "127.0.0.1" in origin for origin in origins):
            raise RuntimeError("ALLOWED_ORIGINS de produ\u00e7\u00e3o n\u00e3o deve apontar para localhost.")
        if "*" in origins:
            logger.warning("ALLOWED_ORIGINS is '*' in production. Use only during the first deploy and replace it with the public HTTPS URL.")
        if not settings.trusted_hosts:
            logger.warning(
                "TRUSTED_HOSTS não está definida em produção. O header Host não é "
                "validado, e o redirect do fluxo OAuth (app/oauth.py) confia nele "
                "sem checagem (SEC-06). Defina TRUSTED_HOSTS com o(s) domínio(s) "
                "público(s), separados por vírgula."
            )


def decode_token_metadata(token: str) -> tuple[str | None, datetime | None]:
    try:
        claims = jwt.get_unverified_claims(token)
    except JWTError:
        return None, None
    user_id = str(claims.get("sub")) if claims.get("sub") else None
    expires_at = None
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        expires_at = datetime.fromtimestamp(exp, UTC)
    return user_id, expires_at


def as_utc_datetime(value: Any) -> datetime | None:
    """Normaliza para datetime em UTC, aceitando também texto ISO-8601.

    As linhas passam por ``normalize_row``, que serializa datetime como string
    ISO. Sem aceitar esse formato aqui, a função devolvia ``None`` para toda
    coluna vinda do banco e as checagens que dependem dela eram silenciosamente
    puladas: o token continuava válido após troca de senha e o bloqueio por
    tentativas de login nunca era aplicado.
    """
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


# Formatos aceitos na importação. Data e data+hora são tentadas na ordem; o
# extrato de cada banco escolhe um destes.
_IMPORT_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y")
_IMPORT_DATETIME_FORMATS = tuple(
    f"{date_format}{separator}{time_format}"
    for date_format in _IMPORT_DATE_FORMATS
    for separator in (" ", "T")
    for time_format in ("%H:%M:%S", "%H:%M")
)


def parse_import_datetime(value: Any) -> tuple[str, str | None]:
    """Normaliza a data do extrato para (AAAA-MM-DD, HH:MM ou None).

    Bancos exportam data com e sem hora, em vários separadores. Guardar a hora
    quando ela existe permite ordenar lançamentos do mesmo dia na ordem real.
    """
    text = str(value or "").strip()
    if not text:
        raise ValueError("Data vazia.")

    for date_format in _IMPORT_DATETIME_FORMATS:
        try:
            parsed = datetime.strptime(text, date_format)
        except ValueError:
            continue
        return parsed.date().isoformat(), parsed.strftime("%H:%M")

    for date_format in _IMPORT_DATE_FORMATS:
        try:
            return datetime.strptime(text, date_format).date().isoformat(), None
        except ValueError:
            continue

    raise ValueError("Data inválida.")


def parse_import_date(value: Any) -> str:
    return parse_import_datetime(value)[0]


def parse_import_time(value: Any) -> str | None:
    """Lê uma coluna de hora separada, quando o arquivo tiver uma."""
    text = str(value or "").strip()
    if not text:
        return None
    for time_format in ("%H:%M:%S", "%H:%M", "%H%M"):
        try:
            return datetime.strptime(text, time_format).strftime("%H:%M")
        except ValueError:
            continue
    return None


def parse_import_type(raw_type: Any, amount: Decimal) -> Literal["income", "expense"]:
    if raw_type is None or str(raw_type).strip() == "":
        return "expense" if amount < 0 else "income"

    value = normalize_duplicate_text(str(raw_type))
    if value in {"income", "entrada", "credito", "crédito", "credit", "receita"}:
        return "income"
    if value in {"expense", "saida", "saída", "debito", "débito", "debit", "despesa"}:
        return "expense"
    return "expense" if amount < 0 else "income"


# CSV-05: só ";" e "," eram reconhecidos. Extratos de alguns bancos e
# planilhas exportadas usam tabulação ou pipe.
_CSV_DELIMITER_CANDIDATES = (";", ",", "\t", "|")


def _count_delimiters(line: str) -> dict[str, int]:
    return {delimiter: line.count(delimiter) for delimiter in _CSV_DELIMITER_CANDIDATES}


def detect_csv_delimiter(sample: str) -> str:
    first_line = sample.splitlines()[0] if sample.splitlines() else ""
    counts = _count_delimiters(first_line)
    best_delimiter = max(counts, key=lambda delimiter: counts[delimiter])
    return best_delimiter if counts[best_delimiter] > 0 else ","


def find_csv_header_line_index(lines: list[str]) -> int:
    """Localiza a linha de cabeçalho real, pulando o preâmbulo que extratos
    bancários costumam trazer antes da tabela (nome do banco, período,
    agência) — CSV-04.

    Heurística: a primeira linha cujo delimitador mais frequente também
    aparece na próxima linha não vazia, com a MESMA contagem de campos.
    Preâmbulo tipicamente não usa o delimitador do arquivo, ou usa em
    quantidade diferente da linha de dados seguinte — a linha de cabeçalho
    de verdade e a primeira linha de dados sempre têm o mesmo número de
    colunas. Sem nenhuma linha assim, cai no comportamento antigo: a
    primeira linha é o cabeçalho.
    """
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        counts = _count_delimiters(line)
        delimiter, count = max(counts.items(), key=lambda item: item[1])
        if count == 0:
            continue
        field_count = len(line.split(delimiter))
        next_line = next((candidate for candidate in lines[index + 1 :] if candidate.strip()), None)
        if next_line is not None and len(next_line.split(delimiter)) == field_count:
            return index
    return 0


def parse_csv_rows(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    text = None
    # CSV-06: cp1252 antes do fallback final. latin-1 nunca levanta
    # UnicodeDecodeError (mapeia todo byte para um caractere), o que fazia um
    # arquivo Windows-1252 (aspas curvas, travessão) "funcionar" decodificado
    # errado em vez de cair no encoding certo.
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = content.decode("latin-1")

    lines = text.splitlines()
    header_index = find_csv_header_line_index(lines)
    delimiter = detect_csv_delimiter(lines[header_index] if lines else "")
    table_text = "\n".join(lines[header_index:])

    reader = csv.DictReader(io.StringIO(table_text), delimiter=delimiter)
    columns = [column.strip() for column in (reader.fieldnames or []) if column and column.strip()]
    if not columns:
        raise HTTPException(status_code=400, detail="CSV sem cabeçalho.")

    rows: list[dict[str, str]] = []
    for index, row in enumerate(reader, start=1):
        if index > CSV_IMPORT_MAX_ROWS:
            raise HTTPException(status_code=400, detail=f"CSV excede o limite de {CSV_IMPORT_MAX_ROWS} linhas.")
        cleaned = {
            str(key or "").strip(): unescape_csv_formula_guard(str(value or "").strip())
            for key, value in row.items()
            if key
        }
        if any(cleaned.values()):
            rows.append(cleaned)
    if not rows:
        raise HTTPException(status_code=400, detail="CSV sem linhas para importar.")
    return columns, rows


def csv_safe_cell(value: Any) -> str:
    text = str(value or "")
    if text and text[0] in CSV_FORMULA_PREFIXES:
        return f"'{text}"
    return text


def unescape_csv_formula_guard(value: str) -> str:
    """Desfaz o apóstrofo de guarda de csv_safe_cell ao reimportar um CSV
    exportado pelo próprio Trevo — sem isto, ele volta como caractere
    literal na descrição (CSV-07). Só remove quando o caractere seguinte é
    exatamente um dos gatilhos de fórmula, o mesmo critério que decide
    adicioná-lo na exportação — não mexe num apóstrofo comum.
    """
    if len(value) >= 2 and value[0] == "'" and value[1] in CSV_FORMULA_PREFIXES:
        return value[1:]
    return value


def serialize_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return round_money(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def normalize_row(row: Any | None) -> dict | None:
    if row is None:
        return None
    return {key: serialize_value(value) for key, value in dict(row).items()}


def normalize_rows(rows: list[Any]) -> list[dict]:
    return [normalize_row(row) or {} for row in rows]


def require_row(row: dict | None, detail: str = "Registro n\u00e3o encontrado.") -> dict:
    if row is None:
        raise HTTPException(status_code=500, detail=detail)
    return row


def clean_text(value: str, field_name: str, max_length: int, required: bool = True) -> str:
    cleaned = value.strip()
    if required and not cleaned:
        raise HTTPException(status_code=400, detail=f"{field_name} \u00e9 obrigat\u00f3rio.")
    if len(cleaned) > max_length:
        raise HTTPException(status_code=400, detail=f"{field_name} excede o tamanho permitido.")
    return cleaned


def validate_hex_color(value: str, field_name: str = "Cor") -> str:
    cleaned = clean_text(value, field_name, 20)
    if not HEX_COLOR_RE.match(cleaned):
        raise HTTPException(status_code=400, detail=f"{field_name} deve usar formato hexadecimal #RRGGBB.")
    return cleaned


def validate_optional_url(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    cleaned = clean_text(value, field_name, 500, required=False)
    if not cleaned:
        return None
    if not (cleaned.startswith("https://") or cleaned.startswith("http://")):
        raise HTTPException(status_code=400, detail=f"{field_name} deve usar http ou https.")
    return cleaned


def detect_profile_photo_extension(content_type: str | None, content: bytes) -> str:
    normalized_type = (content_type or "").split(";")[0].strip().lower()
    extension = PROFILE_PHOTO_ALLOWED_CONTENT_TYPES.get(normalized_type)
    if not extension:
        raise HTTPException(status_code=400, detail="Envie uma imagem JPG, PNG ou WebP.")

    valid_signature = (
        (extension == "jpg" and content.startswith(b"\xff\xd8\xff"))
        or (extension == "png" and content.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension == "webp" and len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP")
    )
    if not valid_signature:
        raise HTTPException(status_code=400, detail="O arquivo enviado não parece ser uma imagem válida.")
    return extension


def delete_profile_photo_file(avatar_url: str | None) -> None:
    # Delegates to the storage layer, which handles both Supabase and local disk
    # references (and ignores external OAuth avatar URLs).
    storage.remove_avatar(avatar_url)


def validate_date_text(value: str, field_name: str) -> str:
    cleaned = clean_text(value, field_name, 10)
    try:
        datetime.strptime(cleaned, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{field_name} inv\u00e1lida.") from None
    return cleaned


def validate_month_text(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = clean_text(value, "M\u00eas", 7)
    try:
        datetime.strptime(cleaned, "%Y-%m")
    except ValueError:
        raise HTTPException(status_code=400, detail="M\u00eas inv\u00e1lido.") from None
    return cleaned


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 255 or not EMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="E-mail inv\u00e1lido.")
    return email


def login_failure_key(email: str) -> str:
    return email_hash(email.strip().lower())


def enforce_login_rate_limit(email: str) -> None:
    key = login_failure_key(email)
    now_dt = datetime.now(UTC)
    now = now_dt.timestamp()
    if storage_available():
        try:
            with db_cursor(commit=True) as cursor:
                cursor.execute(
                    """
                    SELECT attempts, first_attempt_at, blocked_until
                    FROM login_failures_state
                    WHERE identifier_hash = %s
                    """,
                    (key,),
                )
                row = normalize_row(cursor.fetchone())
                if not row:
                    return

                blocked_until = as_utc_datetime(row.get("blocked_until"))
                if blocked_until and blocked_until > now_dt:
                    audit_log("login_rate_limited", None, {"email_hash": key})
                    raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em alguns minutos.")

                first_attempt = as_utc_datetime(row.get("first_attempt_at"))
                if first_attempt and (now_dt - first_attempt).total_seconds() > LOGIN_FAILURE_WINDOW_SECONDS:
                    cursor.execute("DELETE FROM login_failures_state WHERE identifier_hash = %s", (key,))
                    return
        except HTTPException:
            raise
        except Exception:
            logger.exception("Failed to read login failure state")

    entry = login_failures.get(key)
    if not entry:
        return
    blocked_until = float(entry.get("blocked_until") or 0)
    if blocked_until > now:
        audit_log("login_rate_limited", None, {"email_hash": key})
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em alguns minutos.")
    first_attempt = float(entry.get("first_attempt") or 0)
    if now - first_attempt > LOGIN_FAILURE_WINDOW_SECONDS:
        login_failures.pop(key, None)


def record_login_failure(email: str) -> None:
    key = login_failure_key(email)
    now_dt = datetime.now(UTC)
    now = now_dt.timestamp()
    if storage_available():
        try:
            blocked_until_dt = None
            with db_cursor(commit=True) as cursor:
                cursor.execute(
                    """
                    SELECT attempts, first_attempt_at
                    FROM login_failures_state
                    WHERE identifier_hash = %s
                    """,
                    (key,),
                )
                row = normalize_row(cursor.fetchone())
                first_attempt = as_utc_datetime(row.get("first_attempt_at")) if row else None
                if not row or not first_attempt or (now_dt - first_attempt).total_seconds() > LOGIN_FAILURE_WINDOW_SECONDS:
                    attempts = 1
                    first_attempt_at = now_dt
                else:
                    attempts = int(row["attempts"]) + 1
                    first_attempt_at = first_attempt

                if attempts >= LOGIN_MAX_ATTEMPTS:
                    blocked_until_dt = now_dt + timedelta(seconds=LOGIN_FAILURE_WINDOW_SECONDS)

                cursor.execute(
                    """
                    INSERT INTO login_failures_state
                      (identifier_hash, attempts, first_attempt_at, blocked_until)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (identifier_hash)
                    DO UPDATE SET
                      attempts = EXCLUDED.attempts,
                      first_attempt_at = EXCLUDED.first_attempt_at,
                      blocked_until = EXCLUDED.blocked_until
                    """,
                    (key, attempts, first_attempt_at, blocked_until_dt),
                )
            return
        except Exception:
            logger.exception("Failed to persist login failure state")

    entry = login_failures.get(key)
    if not entry or now - float(entry.get("first_attempt") or 0) > LOGIN_FAILURE_WINDOW_SECONDS:
        entry = {"count": 0, "first_attempt": now, "blocked_until": 0}
    entry["count"] = int(entry["count"]) + 1
    if int(entry["count"]) >= LOGIN_MAX_ATTEMPTS:
        entry["blocked_until"] = now + LOGIN_FAILURE_WINDOW_SECONDS
    login_failures[key] = entry


def clear_login_failures(email: str) -> None:
    key = login_failure_key(email)
    login_failures.pop(key, None)
    if not storage_available():
        return
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute("DELETE FROM login_failures_state WHERE identifier_hash = %s", (key,))
    except Exception:
        logger.exception("Failed to clear login failure state")


# Fallback em memória de processo para enforce_ip_rate_limit, usado só quando
# o banco está fora do ar (fail-open não significa "sem limite nenhum"; ver
# a função abaixo). Não substitui a persistência: em serverless cada
# instância tem o seu, mas é melhor que nada durante uma falha transitória.
ip_rate_limit_fallback: dict[str, dict[str, Any]] = {}


def enforce_ip_rate_limit(request: Request, scope: str, max_attempts: int, window_seconds: int) -> None:
    """Limite de tentativas por IP, persistido em Postgres.

    O Limiter do slowapi (ver `limiter` acima) usa armazenamento em memória
    de processo — verificado em runtime. Em serverless cada instância tem o
    próprio contador, então o `@limiter.limit` nas rotas sensíveis
    (cadastro, troca de senha, exclusão de conta, exportação de dados) é, na
    prática, decorativo. Esta função implementa uma janela deslizante simples
    por (scope, ip) na tabela `rate_limit_state`, com o mesmo desenho de
    `enforce_login_rate_limit`.

    Degrada com graça (fail-open): se o banco falhar, cai no fallback em
    memória do processo e nunca bloqueia a requisição por indisponibilidade
    de armazenamento — bloquear login/cadastro porque o banco piscou seria
    pior do que o problema que isto resolve.
    """
    ip_hash = client_ip_hash(request)
    if not ip_hash:
        return
    key = hashlib.sha256(f"{scope}:{ip_hash}".encode()).hexdigest()
    now_dt = datetime.now(UTC)

    if storage_available():
        try:
            with db_cursor(commit=True) as cursor:
                cursor.execute(
                    "SELECT window_start, count FROM rate_limit_state WHERE key_hash = %s",
                    (key,),
                )
                row = normalize_row(cursor.fetchone())
                window_start = as_utc_datetime(row.get("window_start")) if row else None
                if not row or not window_start or (now_dt - window_start).total_seconds() > window_seconds:
                    window_start = now_dt
                    count = 1
                else:
                    count = int(row["count"]) + 1

                cursor.execute(
                    """
                    INSERT INTO rate_limit_state (key_hash, window_start, count)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (key_hash)
                    DO UPDATE SET window_start = EXCLUDED.window_start, count = EXCLUDED.count
                    """,
                    (key, window_start, count),
                )
            if count > max_attempts:
                audit_log("rate_limited", None, {"scope": scope})
                raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente mais tarde.")
            return
        except HTTPException:
            raise
        except Exception:
            logger.exception("Failed to enforce persisted rate limit for scope=%s", scope)

    # Banco indisponível ou fora de serviço: fallback em memória do processo.
    entry = ip_rate_limit_fallback.get(key)
    now = now_dt.timestamp()
    if not entry or now - float(entry.get("window_start") or 0) > window_seconds:
        entry = {"window_start": now, "count": 0}
    entry["count"] = int(entry["count"]) + 1
    ip_rate_limit_fallback[key] = entry
    if entry["count"] > max_attempts:
        audit_log("rate_limited", None, {"scope": scope, "fallback": True})
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente mais tarde.")


def set_auth_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        path="/",
        samesite="lax",
        secure=is_production(),
    )
    # Pair every session with a fresh CSRF token (double-submit).
    issue_csrf_cookie(response)


def token_response_body(request: Request, token: str) -> dict:
    """Corpo de resposta de login/registro (SEC-12).

    O cookie já foi setado por set_auth_cookie — o SPA nunca lê este campo,
    só o cookie. Devolver o token no corpo mesmo assim faz ele trafegar (e
    ser logado por proxies/DevTools) sem necessidade. Clientes de API que
    dependem de Bearer continuam recebendo o token normalmente; o SPA sinaliza
    que só precisa do cookie com o header X-Token-Response: omit.
    """
    if request.headers.get("x-token-response") == "omit":
        return {"token_type": "bearer"}  # nosec B105
    return {"access_token": token, "token_type": "bearer"}  # nosec B105


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(AUTH_COOKIE_NAME, path="/", samesite="lax", secure=is_production())
    response.delete_cookie(CSRF_COOKIE_NAME, path="/", samesite="lax", secure=is_production())


def issue_csrf_cookie(response: Response) -> str:
    # Readable by JS (not HttpOnly) so the SPA can echo it in the X-CSRF-Token
    # header — that echo is what proves same-origin (double-submit).
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        CSRF_COOKIE_NAME,
        token,
        httponly=False,
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        path="/",
        samesite="lax",
        secure=is_production(),
    )
    return token


def revoke_token(token: str, user_id: str | None = None) -> None:
    current_hash = token_hash(token)
    revoked_token_hashes.add(current_hash)
    if not storage_available():
        return
    decoded_user_id, expires_at = decode_token_metadata(token)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO revoked_tokens (token_hash, user_id, expires_at)
                VALUES (%s, %s, %s)
                ON CONFLICT (token_hash)
                DO UPDATE SET revoked_at = NOW(), expires_at = EXCLUDED.expires_at
                """,
                (current_hash, user_id or decoded_user_id, expires_at),
            )
    except Exception:
        logger.exception("Failed to persist revoked token")


def is_token_revoked(token: str) -> bool:
    current_hash = token_hash(token)
    if current_hash in revoked_token_hashes:
        return True
    if not storage_available():
        return False
    try:
        with db_cursor() as cursor:
            cursor.execute(
                """
                SELECT 1
                FROM revoked_tokens
                WHERE token_hash = %s
                  AND (expires_at IS NULL OR expires_at > NOW())
                LIMIT 1
                """,
                (current_hash,),
            )
            return cursor.fetchone() is not None
    except Exception:
        logger.exception("Failed to read revoked token state")
        return False


def public_user(user: dict) -> dict:
    return {
        "id": str(user["id"]),
        "email": user["email"],
        "name": user["name"],
        "avatar_url": storage.resolve_avatar_url(user.get("avatar_url")),
        "send_monthly_summary": bool(user.get("send_monthly_summary", False)),
        "is_active": bool(user["is_active"]),
        "created_at": user.get("created_at"),
        "updated_at": user.get("updated_at"),
        # Exposto para o perfil mostrar qual provedor social já está
        # vinculado à conta — nunca sensível, é o próprio usuário lendo.
        "auth_provider": user.get("auth_provider"),
    }


def get_user_by_email(email: str) -> dict | None:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                   auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            FROM users
            WHERE email = %s
            """,
            (email,),
        )
        return normalize_row(cursor.fetchone())


def get_user_by_oauth(provider: str, subject: str) -> dict | None:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                   auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            FROM users
            WHERE auth_provider = %s AND oauth_subject = %s
            """,
            (provider, subject),
        )
        return normalize_row(cursor.fetchone())


def get_user_by_id(user_id: str) -> dict | None:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                   auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            FROM users
            WHERE id = %s
            """,
            (user_id,),
        )
        return normalize_row(cursor.fetchone())


def oauth_only_password_hash() -> str:
    return hash_password(secrets.token_urlsafe(48))


def resolve_oauth_user(profile: dict[str, str]) -> dict:
    provider = profile["provider"]
    subject = profile["subject"]
    email = normalize_email(profile["email"])
    name = clean_text(profile.get("name") or email.split("@")[0], "Nome", 100)

    existing_oauth = get_user_by_oauth(provider, subject)
    if existing_oauth:
        if not existing_oauth["is_active"]:
            raise HTTPException(status_code=403, detail="Conta desativada.")
        return existing_oauth

    by_email = get_user_by_email(email)
    if by_email:
        if not by_email["is_active"]:
            raise HTTPException(status_code=403, detail="Conta desativada.")
        if by_email.get("oauth_subject") and (
            by_email.get("auth_provider") != provider or str(by_email.get("oauth_subject")) != subject
        ):
            raise HTTPException(status_code=409, detail="E-mail já vinculado a outro provedor social.")
        if by_email.get("hashed_password") and not by_email.get("oauth_subject"):
            # SEC-04: a conta tem senha própria e nunca foi vinculada a
            # nenhum provedor social — vincular automaticamente aqui
            # transferiria a segurança dela inteiramente para a política de
            # verificação de e-mail do provedor OAuth, sem confirmação do
            # dono da conta. A vinculação intencional passa por
            # /api/auth/oauth/{provider}/authorize?link=true, autenticado.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Já existe uma conta com este e-mail. Entre com sua senha e "
                    "vincule o login social pelo seu perfil."
                ),
            )
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                UPDATE users
                SET auth_provider = %s, oauth_subject = %s, name = COALESCE(NULLIF(name, ''), %s)
                WHERE id = %s
                RETURNING id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                          auth_provider, oauth_subject, password_changed_at, created_at, updated_at
                """,
                (provider, subject, name, by_email["id"]),
            )
            return require_row(normalize_row(cursor.fetchone()), "Usuário não encontrado.")

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO users (email, hashed_password, name, auth_provider, oauth_subject)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                          auth_provider, oauth_subject, password_changed_at, created_at, updated_at
                """,
                (email, oauth_only_password_hash(), name, provider, subject),
            )
            user = require_row(normalize_row(cursor.fetchone()), "Usuário não criado.")
            ensure_user_defaults_for_cursor(cursor, user["id"])
    except errors.UniqueViolation:
        linked = get_user_by_oauth(provider, subject) or get_user_by_email(email)
        if linked:
            return linked
        raise HTTPException(status_code=409, detail="Não foi possível vincular conta social.") from None

    audit_log("user_registered_oauth", str(user["id"]), {"provider": provider, "email_hash": email_hash(email)})
    return user


def ensure_user_defaults_for_cursor(cursor, user_id: str) -> None:
    cursor.execute(
        """
        INSERT INTO settings (id, user_id, monthly_income, daily_goal, reserve_amount, currency)
        VALUES (1, %s, 0, 0, 0, 'BRL')
        ON CONFLICT (user_id, id) DO NOTHING
        """,
        (user_id,),
    )
    cursor.executemany(
        """
        INSERT INTO categories (user_id, name, type, color, icon, is_default)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (user_id, name) DO NOTHING
        """,
        [(user_id, name, type_name, color, icon, is_default) for name, type_name, color, icon, is_default in DEFAULT_CATEGORIES],
    )


def ensure_user_defaults(user_id: str) -> None:
    with db_cursor(commit=True) as cursor:
        ensure_user_defaults_for_cursor(cursor, user_id)


def get_current_user(
    response: Response,
    token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
) -> dict:
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token inv\u00e1lido ou expirado.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    # SEC-07: s\u00f3 renova quando a autentica\u00e7\u00e3o veio do cookie (Bearer \u00e9
    # expl\u00edcito \u2014 o cliente de API controla o pr\u00f3prio ciclo de vida do
    # token, e n\u00e3o haveria onde escrever um Set-Cookie de qualquer forma).
    used_cookie = token is None and cookie_token is not None
    token = token or cookie_token
    if not token:
        raise credentials_error
    if is_token_revoked(token):
        raise credentials_error
    try:
        payload = jwt.decode(token, get_jwt_secret(), algorithms=[JWT_ALGORITHM])
        subject = payload.get("sub")
        if not subject:
            raise credentials_error
        user_uuid = UUID(str(subject))
        issued_at_claim = payload.get("iat")
        expires_at_claim = payload.get("exp")
    except (JWTError, ValueError):
        raise credentials_error from None

    user = get_user_by_id(str(user_uuid))
    if not user or not user["is_active"]:
        raise credentials_error
    password_changed_at = as_utc_datetime(user.get("password_changed_at"))
    if password_changed_at:
        if not isinstance(issued_at_claim, (int, float)):
            raise credentials_error
        issued_at = datetime.fromtimestamp(float(issued_at_claim), UTC)
        if issued_at < password_changed_at:
            raise credentials_error

    if used_cookie and isinstance(expires_at_claim, (int, float)):
        # Renova\u00e7\u00e3o deslizante: reemite o cookie quando falta menos de 25% da
        # validade. Uso cont\u00ednuo nunca deixa a sess\u00e3o chegar perto de
        # expirar; parada, expira em at\u00e9 ACCESS_TOKEN_EXPIRE_HOURS ap\u00f3s o
        # \u00faltimo request. N\u00e3o revoga o token antigo \u2014 ele j\u00e1 vale at\u00e9 o
        # pr\u00f3prio exp original, e revogar aqui derrubaria outras abas com o
        # cookie ainda n\u00e3o atualizado (Set-Cookie n\u00e3o \u00e9 instant\u00e2neo entre
        # abas).
        total_seconds = ACCESS_TOKEN_EXPIRE_HOURS * 3600
        remaining_seconds = expires_at_claim - datetime.now(UTC).timestamp()
        if total_seconds > 0 and remaining_seconds < total_seconds * 0.25:
            set_auth_cookie(response, create_access_token(user["id"]))

    return public_user(user)


def get_optional_current_user(
    response: Response,
    token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
) -> dict | None:
    """Como get_current_user, mas devolve None em vez de 401 sem sessão.

    Usado por rotas que precisam se comportar diferente se a requisição já
    está autenticada, sem tornar a autenticação obrigatória — o link OAuth
    (?link=true) exige sessão; o authorize normal não.
    """
    try:
        return get_current_user(response, token=token, cookie_token=cookie_token)
    except HTTPException:
        return None


def link_oauth_identity_to_user(user_id: str, profile: dict[str, str]) -> None:
    """Vincula um provedor social à conta JÁ AUTENTICADA que iniciou o pedido.

    Ao contrário de resolve_oauth_user (usado no login), esta função nunca
    decide por conta própria a QUEM vincular — o usuário já está
    identificado pela sessão que abriu o fluxo (ver oauth_authorize com
    link=true), fechando o ponto de SEC-04.
    """
    provider = profile["provider"]
    subject = profile["subject"]
    existing = get_user_by_oauth(provider, subject)
    if existing and str(existing["id"]) != str(user_id):
        raise HTTPException(status_code=409, detail="Esta conta social já está vinculada a outro usuário.")
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "UPDATE users SET auth_provider = %s, oauth_subject = %s WHERE id = %s",
                (provider, subject, user_id),
            )
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Esta conta social já está vinculada a outro usuário.") from None
    audit_log("oauth_linked", user_id, {"provider": provider})


def get_settings(user_id: str) -> dict:
    return request_cached(("settings", user_id), lambda: _compute_settings(user_id))


def _compute_settings(user_id: str) -> dict:
    with db_cursor() as cursor:
        cursor.execute("SELECT * FROM settings WHERE user_id = %s AND id = 1", (user_id,))
        row = normalize_row(cursor.fetchone())
    if row:
        return row

    ensure_user_defaults(user_id)
    with db_cursor() as cursor:
        cursor.execute("SELECT * FROM settings WHERE user_id = %s AND id = 1", (user_id,))
        row = normalize_row(cursor.fetchone())
    if not row:
        raise HTTPException(status_code=500, detail="Configura\u00e7\u00f5es n\u00e3o encontradas.")
    return row


def get_effective_income(user_settings: dict, inflow: Any) -> Decimal:
    """Renda efetiva do m\u00eas: o maior entre a renda configurada e o que j\u00e1
    entrou em lan\u00e7amentos de receita.

    Antes, get_dashboard, _compute_goals e calculate_score somavam
    monthly_income (renda configurada em Configura\u00e7\u00f5es) a inflow (soma de
    TODAS as transa\u00e7\u00f5es de tipo income do m\u00eas) sem checar se eram a mesma
    coisa. Quem lan\u00e7a o sal\u00e1rio como transa\u00e7\u00e3o de entrada \u2014 o gesto natural,
    e a categoria padr\u00e3o "Sal\u00e1rio" existe exatamente para isso \u2014 tinha a
    renda contada duas vezes: or\u00e7amento dispon\u00edvel e meta di\u00e1ria dobravam, o
    Ritmo Score inflava e os alertas de estouro paravam de disparar (DOM-05).

    monthly_income passa a ser tratado como renda ESPERADA: se o que j\u00e1
    entrou no m\u00eas cobre ou supera esse valor, usa o que entrou; caso
    contr\u00e1rio usa o configurado (para quem ainda n\u00e3o lan\u00e7ou a renda do m\u00eas
    corrente, ou lan\u00e7a s\u00f3 parte dela como transa\u00e7\u00e3o).
    """
    monthly_income = round_money(user_settings.get("monthly_income") or 0)
    return max(monthly_income, round_money(inflow))


def list_categories(user_id: str) -> list[dict]:
    return request_cached(("categories", user_id), lambda: _compute_categories(user_id))


def _compute_categories(user_id: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM categories
            WHERE user_id = %s
              AND COALESCE(is_active, TRUE) = TRUE
            ORDER BY type ASC, name ASC
            """,
            (user_id,),
        )
        return normalize_rows(cursor.fetchall())


def list_cards(user_id: str) -> list[dict]:
    return request_cached(("cards", user_id), lambda: _compute_cards(user_id))


def _compute_cards(user_id: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM cards
            WHERE user_id = %s
            ORDER BY created_at ASC, id ASC
            """,
            (user_id,),
        )
        return normalize_rows(cursor.fetchall())


def list_transactions(
    user_id: str,
    month: str | None = None,
    transaction_type: str | None = None,
    category_id: int | None = None,
    payment_method: str | None = None,
    source: str | None = None,
    card_id: int | None = None,
    search: str | None = None,
) -> list[dict]:
    pattern = f"%{search}%" if search else None
    query = """
        SELECT t.*, c.name AS category_name, c.color AS category_color, cards.name AS card_name
        FROM transactions t
        LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
        LEFT JOIN cards ON cards.id = t.card_id AND cards.user_id = t.user_id
        WHERE t.user_id = %s
          AND (%s IS NULL OR COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s)
          AND (%s IS NULL OR t.type = %s)
          AND (%s IS NULL OR t.category_id = %s)
          AND (%s IS NULL OR t.payment_method = %s)
          AND (%s IS NULL OR t.source = %s)
          AND (%s IS NULL OR t.card_id = %s)
          AND (
            %s IS NULL
            OR lower(t.title) LIKE lower(%s)
            OR lower(COALESCE(t.raw_description, '')) LIKE lower(%s)
          )
        ORDER BY t.transaction_date DESC, t.id DESC
        LIMIT 250
    """
    params = (
        user_id,
        month,
        month,
        transaction_type,
        transaction_type,
        category_id,
        category_id,
        payment_method,
        payment_method,
        source,
        source,
        card_id,
        card_id,
        pattern,
        pattern,
        pattern,
    )

    with db_cursor() as cursor:
        cursor.execute(query, params)
        return normalize_rows(cursor.fetchall())


def get_cards_summary(user_id: str, month: str) -> list[dict]:
    # PERF-03: bootstrap() e get_reports_summary() chamam get_dashboard()
    # (que j\u00e1 pede isto por dentro) E get_cards_summary() de novo com os
    # mesmos argumentos \u2014 cache de escopo de request evita computar duas
    # vezes dentro do mesmo request, sem risco de servir dado velho entre
    # requests diferentes.
    return request_cached(("cards_summary", user_id, month), lambda: _compute_cards_summary(user_id, month))


def _compute_cards_summary(user_id: str, month: str) -> list[dict]:
    """Resumo de todos os cart\u00f5es do usu\u00e1rio para o m\u00eas.

    PERF-02: a vers\u00e3o anterior fazia 2 queries por cart\u00e3o + 1 por grupo de
    parcelamento (at\u00e9 ~70 idas ao banco com 3 cart\u00f5es e 20 grupos), cada uma
    abrindo a pr\u00f3pria conex\u00e3o em serverless. Agora s\u00e3o sempre 4 queries no
    total, batidas por card_id \u2014 n\u00e3o escala com o n\u00famero de cart\u00f5es/grupos.
    """
    cards = list_cards(user_id)
    if not cards:
        return []
    card_ids = [int(card["id"]) for card in cards]

    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT card_id, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND card_id = ANY(%s)
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY card_id
            """,
            (user_id, card_ids, month),
        )
        invoice_by_card = {row["card_id"]: round_money(row["total"]) for row in normalize_rows(cursor.fetchall())}

        # Parcela corrente de cada grupo (existe uma linha com billing_month
        # = m\u00eas pedido).
        cursor.execute(
            """
            SELECT card_id, installment_group, installment_number, total_installments, title, amount
            FROM transactions
            WHERE user_id = %s
              AND card_id = ANY(%s)
              AND installment_group IS NOT NULL
              AND billing_month = %s
            """,
            (user_id, card_ids, month),
        )
        current_by_group = {
            (row["card_id"], row["installment_group"]): row for row in normalize_rows(cursor.fetchall())
        }

        # Para grupos sem parcela no m\u00eas corrente: a mais antiga entre as
        # faturas futuras, mais quantas restam. DISTINCT ON pega a linha de
        # billing_month mais cedo por grupo; a janela conta todo o grupo.
        cursor.execute(
            """
            SELECT DISTINCT ON (card_id, installment_group)
              card_id, installment_group, title, amount,
              COUNT(*) OVER (PARTITION BY card_id, installment_group) AS future_count
            FROM transactions
            WHERE user_id = %s
              AND card_id = ANY(%s)
              AND installment_group IS NOT NULL
              AND billing_month >= %s
            ORDER BY card_id, installment_group, billing_month ASC
            """,
            (user_id, card_ids, month),
        )
        future_by_group = {
            (row["card_id"], row["installment_group"]): row for row in normalize_rows(cursor.fetchall())
        }

        cursor.execute(
            """
            SELECT card_id, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS remaining_installments
            FROM transactions
            WHERE user_id = %s
              AND card_id = ANY(%s)
              AND type = 'expense'
              AND installment_group IS NOT NULL
              AND billing_month >= %s
            GROUP BY card_id
            """,
            (user_id, card_ids, month),
        )
        commitment_by_card = {
            row["card_id"]: {
                "committedLimit": round_money(row["total"]),
                "remainingInstallments": int(row["remaining_installments"]),
            }
            for row in normalize_rows(cursor.fetchall())
        }

    groups_by_card: dict[int, set[str]] = {}
    for card_id, group in current_by_group:
        groups_by_card.setdefault(card_id, set()).add(group)
    for card_id, group in future_by_group:
        groups_by_card.setdefault(card_id, set()).add(group)

    result: list[dict] = []
    for card in cards:
        # list_cards() agora é cacheado por request (request_cached) — os
        # dicts aqui são compartilhados com quem mais chamar list_cards()
        # nesta mesma request, então os campos calculados abaixo vão numa
        # cópia, nunca no dict original.
        card = dict(card)
        card_id = int(card["id"])
        invoice = invoice_by_card.get(card_id, Decimal("0"))

        active_installments: list[dict] = []
        for group in sorted(groups_by_card.get(card_id, ())):
            key = (card_id, group)
            current_row = current_by_group.get(key)
            if current_row:
                active_installments.append(
                    {
                        "title": current_row["title"],
                        "installmentLabel": f'{current_row["installment_number"]}/{current_row["total_installments"]}',
                        "remaining": current_row["total_installments"] - current_row["installment_number"],
                        "amount": round_money(current_row["amount"]),
                    }
                )
                continue
            future_row = future_by_group.get(key)
            if future_row:
                active_installments.append(
                    {
                        "title": future_row["title"],
                        "installmentLabel": "\u00c0 frente",
                        "remaining": int(future_row["future_count"]),
                        "amount": round_money(future_row["amount"]),
                    }
                )

        card["invoice"] = invoice
        card["availableCredit"] = round_money(card["credit_limit"] - invoice)
        commitment = commitment_by_card.get(card_id, {"committedLimit": Decimal("0"), "remainingInstallments": 0})
        card["committedLimit"] = commitment["committedLimit"]
        card["remainingInstallments"] = commitment["remainingInstallments"]
        usage = (invoice / round_money(card["credit_limit"])) if round_money(card["credit_limit"]) > 0 else Decimal("0")
        card["invoiceAlert"] = usage > Decimal("0.8")
        card["activeInstallmentsCount"] = len(active_installments)
        card["activeInstallments"] = active_installments
        result.append(card)

    return result


def get_card_for_user(user_id: str, card_id: int) -> dict:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM cards
            WHERE user_id = %s AND id = %s
            """,
            (user_id, card_id),
        )
        card = normalize_row(cursor.fetchone())

    if not card:
        raise HTTPException(status_code=404, detail="Cart\u00e3o n\u00e3o encontrado.")
    return card


def get_card_pin_row(user_id: str, card_id: int) -> dict | None:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, card_id, user_id, pin_hash, created_at
            FROM card_pins
            WHERE user_id = %s AND card_id = %s
            """,
            (user_id, card_id),
        )
        return normalize_row(cursor.fetchone())


def get_invoice_totals_by_card(user_id: str, month: str) -> dict[int, Decimal]:
    """Fatura de todos os cartões do mês numa query só.

    calculate_score e get_alerts_for_month somavam a fatura cartão a cartão
    via get_invoice_total (uma query cada) — com N cartões, N queries em cada
    função. Aqui é sempre 1 query batida por card_id, igual ao padrão já
    usado em _compute_cards_summary.
    """
    return request_cached(
        ("invoice_totals_by_card", user_id, month), lambda: _compute_invoice_totals_by_card(user_id, month)
    )


def _compute_invoice_totals_by_card(user_id: str, month: str) -> dict[int, Decimal]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT card_id, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND card_id IS NOT NULL
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY card_id
            """,
            (user_id, month),
        )
        rows = normalize_rows(cursor.fetchall())
    return {int(row["card_id"]): round_money(row["total"]) for row in rows}


def get_invoice_total(user_id: str, card_id: int, month: str) -> Decimal:
    return request_cached(("invoice_total", user_id, card_id, month), lambda: _compute_invoice_total(user_id, card_id, month))


def _compute_invoice_total(user_id: str, card_id: int, month: str) -> Decimal:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND card_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            """,
            (user_id, card_id, month),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Fatura n\u00e3o encontrada.")
    return round_money(row["total"])


def get_active_installments(user_id: str, card_id: int, month: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT title, amount, billing_month, installment_number, total_installments
            FROM transactions
            WHERE user_id = %s
              AND card_id = %s
              AND installment_group IS NOT NULL
              AND billing_month = %s
            ORDER BY transaction_date DESC, id DESC
            """,
            (user_id, card_id, month),
        )
        rows = normalize_rows(cursor.fetchall())

    installments: list[dict] = []
    for row in rows:
        total = int(row["total_installments"] or 0)
        current = int(row["installment_number"] or 0)
        remaining = max(total - current, 0) if total else 0
        progress = round((current / total) * 100, 2) if total else 0
        installments.append(
            {
                "title": row["title"],
                "amount": round_money(row["amount"]),
                "billing_month": row["billing_month"],
                "installment_number": current,
                "total_installments": total,
                "installment_label": f"{current}/{total}" if total else "-",
                "remaining": remaining,
                "progress": progress,
            }
        )
    return installments


def simulate_card_invoices(
    user_id: str,
    card_id: int,
    start_month: str,
    months: int,
    category_id: int | None = None,
) -> list[dict]:
    """PERF-06: uma query com GROUP BY para todos os meses simulados, em vez
    de uma query por m\u00eas (at\u00e9 24 idas ao banco em simula\u00e7\u00f5es mais longas)."""
    month_keys = [add_months(start_month, offset) for offset in range(months)]
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(billing_month, substring(transaction_date from 1 for 7)) AS month_key,
                   COALESCE(SUM(amount), 0) AS total, COUNT(*) AS installments_count
            FROM transactions
            WHERE user_id = %s
              AND card_id = %s
              AND type = 'expense'
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
              AND (%s IS NULL OR category_id = %s)
            GROUP BY month_key
            """,
            (user_id, card_id, month_keys, category_id, category_id),
        )
        totals_by_month = {row["month_key"]: row for row in normalize_rows(cursor.fetchall())}

    result: list[dict] = []
    for month_key in month_keys:
        row = totals_by_month.get(month_key)
        total = round_money(row["total"]) if row else Decimal("0")
        count = int(row["installments_count"]) if row else 0
        result.append(
            {
                "month": month_key,
                "projected_total": total,
                "projectedTotal": total,
                "installments_count": count,
                "itemsCount": count,
            }
        )
    return result


def get_card_commitment(user_id: str, card_id: int, month: str, category_id: int | None = None) -> dict:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total, COUNT(*) AS remaining_installments
            FROM transactions
            WHERE user_id = %s
              AND card_id = %s
              AND type = 'expense'
              AND installment_group IS NOT NULL
              AND billing_month >= %s
              AND (%s IS NULL OR category_id = %s)
            """,
            (user_id, card_id, month, category_id, category_id),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Comprometimento do cartão não encontrado.")
    return {
        "committedLimit": round_money(row["total"]),
        "remainingInstallments": int(row["remaining_installments"]),
    }


def get_grouped_installment_purchases(user_id: str, card_id: int, month: str, category_id: int | None = None) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              installment_group,
              MIN(title) AS title,
              MIN(transaction_date) AS purchase_date,
              MIN(billing_month) AS first_open_month,
              MAX(billing_month) AS last_month,
              MAX(total_installments) AS total_installments,
              COUNT(*) AS remaining_installments,
              COALESCE(SUM(amount), 0) AS remaining_amount
            FROM transactions
            WHERE user_id = %s
              AND card_id = %s
              AND type = 'expense'
              AND installment_group IS NOT NULL
              AND billing_month >= %s
              AND (%s IS NULL OR category_id = %s)
            GROUP BY installment_group
            ORDER BY first_open_month ASC, title ASC
            """,
            (user_id, card_id, month, category_id, category_id),
        )
        rows = normalize_rows(cursor.fetchall())

    return [
        {
            "group": row["installment_group"],
            "title": row["title"],
            "purchaseDate": row["purchase_date"],
            "firstOpenMonth": row["first_open_month"],
            "lastMonth": row["last_month"],
            "totalInstallments": int(row["total_installments"] or 0),
            "remainingInstallments": int(row["remaining_installments"] or 0),
            "remainingAmount": round_money(row["remaining_amount"]),
        }
        for row in rows
    ]


def get_recent_card_transactions(user_id: str, card_id: int) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT t.id, t.title, t.amount, t.type, t.payment_method, t.transaction_date, t.notes,
                   t.billing_month, t.installment_number, t.total_installments, t.created_at,
                   c.name AS category_name, c.color AS category_color
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s AND t.card_id = %s
            ORDER BY t.transaction_date DESC, t.id DESC
            LIMIT 20
            """,
            (user_id, card_id),
        )
        return normalize_rows(cursor.fetchall())


def get_unlocked_card_details(
    user_id: str,
    card_id: int,
    month: str,
    include_token: bool = False,
    category_id: int | None = None,
) -> dict:
    card = get_card_for_user(user_id, card_id)
    invoice = get_invoice_total(user_id, card_id, month)
    commitment = get_card_commitment(user_id, card_id, month, category_id)
    usage = (invoice / round_money(card["credit_limit"])) if round_money(card["credit_limit"]) > 0 else Decimal("0")
    invoice_alert = None
    if usage > Decimal("0.8"):
        invoice_alert = {
            "type": "danger" if usage > Decimal("0.9") else "warning",
            "message": "Fatura alta para o limite do cartão.",
            "usagePercent": int((usage * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP)),
        }
    details = {
        "id": card["id"],
        "name": card["name"],
        "brand": card["brand"],
        "last_four": card["last_four"],
        "credit_limit": round_money(card["credit_limit"]),
        "invoice": invoice,
        "available_credit": round_money(card["credit_limit"] - invoice),
        "committed_limit": commitment["committedLimit"],
        "committedLimit": commitment["committedLimit"],
        "remainingInstallments": commitment["remainingInstallments"],
        "closing_day": card["closing_day"],
        "due_day": card["due_day"],
        "active_installments": get_active_installments(user_id, card_id, month),
        "groupedInstallments": get_grouped_installment_purchases(user_id, card_id, month, category_id),
        "invoiceAlert": invoice_alert,
        "upcoming_invoices": simulate_card_invoices(user_id, card_id, month, 12, category_id),
        "recent_transactions": get_recent_card_transactions(user_id, card_id),
        "is_unlocked": True,
    }
    if include_token:
        token, expires_at = create_card_unlock_session(user_id, card_id)
        details["unlock_token"] = token
        details["unlock_expires_at"] = expires_at.isoformat()
    return details


def card_pin_failure_key(user_id: str, card_id: int) -> str:
    return f"{user_id}:{card_id}"


def enforce_card_pin_rate_limit(user_id: str, card_id: int) -> None:
    key = card_pin_failure_key(user_id, card_id)
    now = datetime.now(UTC).timestamp()
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                SELECT attempts, first_attempt_at, blocked_until
                FROM card_pin_failures_state
                WHERE user_id = %s AND card_id = %s
                """,
                (user_id, card_id),
            )
            row = normalize_row(cursor.fetchone())
            if not row:
                return

            blocked_until = row.get("blocked_until")
            if isinstance(blocked_until, datetime) and blocked_until > datetime.now(UTC):
                raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em 5 minutos.")

            first_attempt = row.get("first_attempt_at")
            if isinstance(first_attempt, datetime) and (
                datetime.now(UTC) - first_attempt
            ).total_seconds() > PIN_FAILURE_WINDOW_SECONDS:
                cursor.execute(
                    "DELETE FROM card_pin_failures_state WHERE user_id = %s AND card_id = %s",
                    (user_id, card_id),
                )
        return

    entry = card_pin_failures.get(key)
    if not entry:
        return

    blocked_until = float(entry.get("blocked_until") or 0)
    if blocked_until > now:
        raise HTTPException(status_code=429, detail="Muitas tentativas. Tente novamente em 5 minutos.")

    first_attempt = float(entry.get("first_attempt") or 0)
    if now - first_attempt > PIN_FAILURE_WINDOW_SECONDS:
        card_pin_failures.pop(key, None)


def record_card_pin_failure(user_id: str, card_id: int) -> int:
    key = card_pin_failure_key(user_id, card_id)
    now = datetime.now(UTC).timestamp()
    if storage_available():
        now_dt = datetime.now(UTC)
        blocked_until_dt = None
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                SELECT attempts, first_attempt_at
                FROM card_pin_failures_state
                WHERE user_id = %s AND card_id = %s
                """,
                (user_id, card_id),
            )
            row = normalize_row(cursor.fetchone())
            if not row or (
                isinstance(row.get("first_attempt_at"), datetime)
                and (now_dt - row["first_attempt_at"]).total_seconds() > PIN_FAILURE_WINDOW_SECONDS
            ):
                attempts = 1
                first_attempt_at = now_dt
            else:
                attempts = int(row["attempts"]) + 1
                first_attempt_at = row["first_attempt_at"]

            attempts_remaining = max(PIN_MAX_ATTEMPTS - attempts, 0)
            if attempts >= PIN_MAX_ATTEMPTS:
                blocked_until_dt = now_dt + timedelta(seconds=PIN_FAILURE_WINDOW_SECONDS)

            cursor.execute(
                """
                INSERT INTO card_pin_failures_state
                  (user_id, card_id, attempts, first_attempt_at, blocked_until)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, card_id)
                DO UPDATE SET
                  attempts = EXCLUDED.attempts,
                  first_attempt_at = EXCLUDED.first_attempt_at,
                  blocked_until = EXCLUDED.blocked_until
                """,
                (user_id, card_id, attempts, first_attempt_at, blocked_until_dt),
            )
        return attempts_remaining

    entry = card_pin_failures.get(key)
    if not entry or now - float(entry.get("first_attempt") or 0) > PIN_FAILURE_WINDOW_SECONDS:
        entry = {"count": 0, "first_attempt": now, "blocked_until": 0}

    entry["count"] = int(entry["count"]) + 1
    attempts_remaining = max(PIN_MAX_ATTEMPTS - int(entry["count"]), 0)
    if int(entry["count"]) >= PIN_MAX_ATTEMPTS:
        entry["blocked_until"] = now + PIN_FAILURE_WINDOW_SECONDS
    card_pin_failures[key] = entry
    return attempts_remaining


def clear_card_pin_failures(user_id: str, card_id: int) -> None:
    card_pin_failures.pop(card_pin_failure_key(user_id, card_id), None)
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM card_pin_failures_state WHERE user_id = %s AND card_id = %s",
                (user_id, card_id),
            )


def invalidate_card_unlock_sessions(user_id: str, card_id: int) -> None:
    for token, session in list(card_unlock_sessions.items()):
        if session["user_id"] == user_id and int(session["card_id"]) == card_id:
            card_unlock_sessions.pop(token, None)
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "DELETE FROM card_unlock_sessions_state WHERE user_id = %s AND card_id = %s",
                (user_id, card_id),
            )


def create_card_unlock_session(user_id: str, card_id: int) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(UTC) + timedelta(seconds=CARD_UNLOCK_SECONDS)
    card_unlock_sessions[token] = {
        "user_id": user_id,
        "card_id": card_id,
        "expires_at": expires_at,
    }
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO card_unlock_sessions_state (token_hash, user_id, card_id, expires_at)
                VALUES (%s, %s, %s, %s)
                """,
                (token_hash(token), user_id, card_id, expires_at),
            )
    return token, expires_at


def verify_card_unlock_session(user_id: str, card_id: int, token: str) -> None:
    if storage_available():
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                SELECT user_id, card_id, expires_at
                FROM card_unlock_sessions_state
                WHERE token_hash = %s
                """,
                (token_hash(token),),
            )
            row = normalize_row(cursor.fetchone())
            if not row:
                raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o expirado.")
            expires_at = row["expires_at"]
            if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
                cursor.execute("DELETE FROM card_unlock_sessions_state WHERE token_hash = %s", (token_hash(token),))
                raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o expirado.")
            if str(row["user_id"]) != user_id or int(row["card_id"]) != card_id:
                raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o inv\u00e1lido.")
        return

    session = card_unlock_sessions.get(token)
    if not session:
        raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o expirado.")

    expires_at = session["expires_at"]
    if not isinstance(expires_at, datetime) or expires_at <= datetime.now(UTC):
        card_unlock_sessions.pop(token, None)
        raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o expirado.")

    if session["user_id"] != user_id or int(session["card_id"]) != card_id:
        raise HTTPException(status_code=401, detail="Desbloqueio do cart\u00e3o inv\u00e1lido.")


def get_dashboard(user_id: str, month: str) -> dict:
    user_settings = get_settings(user_id)

    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS inflow,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            """,
            (user_id, month),
        )
        totals = require_row(normalize_row(cursor.fetchone()), "Totais do dashboard n\u00e3o encontrados.")

        cursor.execute(
            """
            SELECT c.name, c.color, COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND t.type = 'expense'
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            GROUP BY c.name, c.color
            HAVING COALESCE(SUM(t.amount), 0) > 0
            ORDER BY total DESC
            """,
            (user_id, month),
        )
        category_breakdown = normalize_rows(cursor.fetchall())

        # Os 12 meses da s\u00e9rie saem de uma \u00fanica agrega\u00e7\u00e3o; meses sem lan\u00e7amento
        # entram zerados no preenchimento abaixo.
        months = [add_months(month, idx - 11) for idx in range(12)]
        cursor.execute(
            """
            SELECT
              COALESCE(billing_month, substring(transaction_date from 1 for 7)) AS month_key,
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS inflow,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
            GROUP BY month_key
            """,
            (user_id, months),
        )
        trend_by_month = {row["month_key"]: row for row in normalize_rows(cursor.fetchall())}
        monthly_trend: list[dict] = []
        for month_key in months:
            row = trend_by_month.get(month_key)
            inflow = round_money(row["inflow"]) if row else Decimal("0.00")
            outflow = round_money(row["outflow"]) if row else Decimal("0.00")
            monthly_trend.append(
                {
                    "month": month_key,
                    "label": format_month_label(month_key),
                    "inflow": inflow,
                    "outflow": outflow,
                    "net": round_money(inflow - outflow),
                }
            )

        cursor.execute(
            """
            SELECT t.*, c.name AS category_name, c.color AS category_color, cards.name AS card_name
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            LEFT JOIN cards ON cards.id = t.card_id AND cards.user_id = t.user_id
            WHERE t.user_id = %s
            ORDER BY t.transaction_date DESC, t.id DESC
            LIMIT 12
            """,
            (user_id,),
        )
        recent_transactions = normalize_rows(cursor.fetchall())

        cursor.execute(
            """
            SELECT payment_method, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY payment_method
            ORDER BY total DESC
            """,
            (user_id, month),
        )
        payment_method_breakdown = normalize_rows(cursor.fetchall())

        previous_month = add_months(month, -1)
        cursor.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS inflow,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            """,
            (user_id, previous_month),
        )
        previous_totals = require_row(normalize_row(cursor.fetchone()), "Totais do m\u00eas anterior n\u00e3o encontrados.")

    inflow = round_money(totals["inflow"])
    outflow = round_money(totals["outflow"])
    base_income = round_money(user_settings["monthly_income"] or 0)
    reserve_amount = round_money(user_settings.get("reserve_amount") or 0)
    # DOM-05: balance e o percentual comprometido usam a renda EFETIVA (o
    # maior entre configurada e o que já entrou), não a soma das duas — ver
    # get_effective_income.
    effective_income = get_effective_income(user_settings, inflow)
    balance = round_money(effective_income - outflow)
    goals = get_goals(user_id, month)
    previous_inflow = round_money(previous_totals["inflow"])
    previous_outflow = round_money(previous_totals["outflow"])
    previous_balance = round_money(get_effective_income(user_settings, previous_inflow) - previous_outflow)
    salary_base = effective_income
    committed_percent = (
        int(((outflow + reserve_amount) / salary_base * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))
        if salary_base > 0
        else 0
    )

    return {
        "month": month,
        "monthlyIncome": base_income,
        "salaryBase": base_income,
        "extraIncome": inflow,
        "inflow": inflow,
        "outflow": outflow,
        "balance": balance,
        "projectedBalance": balance,
        "salaryCommittedPercent": committed_percent,
        "availableToday": round_money(goals["allowedRemaining"] / Decimal(max(goals["totalDays"] - goals["progressDay"] + 1, 1))),
        "rhythmStatus": goals["goalStatus"],
        "closingProjection": goals["projectedClosing"],
        "reserve": {
            "monthlyPlanned": reserve_amount,
            "goalAmount": round_money(user_settings.get("reserve_goal_amount") or 0),
            "currentAmount": round_money(user_settings.get("reserve_current_amount") or 0),
        },
        "previousMonthComparison": {
            "month": previous_month,
            "inflow": previous_inflow,
            "outflow": previous_outflow,
            "balance": previous_balance,
            "balanceDelta": round_money(balance - previous_balance),
            "outflowDelta": round_money(outflow - previous_outflow),
        },
        "categoryBreakdown": category_breakdown,
        "paymentMethodBreakdown": payment_method_breakdown,
        "cardInvoices": get_cards_summary(user_id, month),
        "monthlyTrend": monthly_trend,
        "recentTransactions": recent_transactions,
    }


def get_goals(user_id: str, month: str) -> dict:
    return request_cached(("goals", user_id, month), lambda: _compute_goals(user_id, month))


def _compute_goals(user_id: str, month: str) -> dict:
    user_settings = get_settings(user_id)
    year, month_num = [int(part) for part in month.split("-")]

    from calendar import monthrange

    total_days = monthrange(year, month_num)[1]
    # DOM-04: "hoje" e "mês atual" do ponto de vista do usuário, não de UTC —
    # ver app/shared/clock.py.
    today = clock.today()
    current_month = today.strftime("%Y-%m")
    if month < current_month:
        progress_day = total_days
    elif month > current_month:
        progress_day = 1
    else:
        progress_day = min(today.day, total_days)
    cutoff_date = date(year, month_num, progress_day).isoformat()

    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              substring(transaction_date from 9 for 2) AS day,
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount ELSE 0 END), 0) AS income,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount ELSE 0 END), 0) AS expense
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY day
            """,
            (user_id, month),
        )
        rows = normalize_rows(cursor.fetchall())
        cursor.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS inflow,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            """,
            (user_id, month),
        )
        totals = require_row(normalize_row(cursor.fetchone()), "Totais das metas não encontrados.")
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
              AND transaction_date <= %s
            """,
            (user_id, month, cutoff_date),
        )
        current_outflow_row = require_row(normalize_row(cursor.fetchone()), "Gasto atual não encontrado.")

    # FIN-02: uma parcela ou compra pós-fechamento pode ter transaction_date
    # em um mês diferente do billing_month (a fatura em que ela cai). O dia de
    # origem às vezes nem existe no mês exibido (dia 31 comprado em agosto,
    # faturado em setembro, que tem 30 dias) — concentra no último dia em vez
    # de descartar, para o somatório do calendário nunca divergir do total do
    # mês exibido em outro lugar da tela.
    day_map: dict[int, dict[str, Decimal]] = {}
    for row in rows:
        day_number = min(int(row["day"]), total_days)
        bucket = day_map.setdefault(day_number, {"income": Decimal("0"), "expense": Decimal("0")})
        bucket["income"] = round_money(bucket["income"] + round_money(row["income"]))
        bucket["expense"] = round_money(bucket["expense"] + round_money(row["expense"]))
    days: list[dict] = []
    legacy_daily_goal = round_money(user_settings["daily_goal"])
    reserve_amount = round_money(user_settings.get("reserve_amount") or 0)
    inflow = round_money(totals["inflow"])
    outflow = round_money(totals["outflow"])
    outflow_to_today = round_money(current_outflow_row["outflow"])
    # DOM-05: renda efetiva (o maior entre configurada e o que já entrou),
    # não a soma das duas — ver get_effective_income.
    available_budget = round_money(get_effective_income(user_settings, inflow) - reserve_amount)
    recommended_daily_goal = round_money(available_budget / Decimal(total_days)) if available_budget > 0 else Decimal("0.00")
    target_daily_goal = legacy_daily_goal if legacy_daily_goal > 0 else recommended_daily_goal
    # A média fica sem arredondar para projetar; arredondar antes de multiplicar
    # pelos dias do mês espalhava o erro (900 gastos em 31 dias projetavam 899,93).
    average_spend_raw = (outflow_to_today / Decimal(progress_day)) if progress_day > 0 else Decimal("0")
    current_average_spend = round_money(average_spend_raw)
    projected_closing = round_money(average_spend_raw * Decimal(total_days))
    allowed_remaining = round_money(available_budget - outflow_to_today)

    if available_budget <= 0 and projected_closing > 0:
        status_name = "red"
    elif available_budget <= 0 or projected_closing <= available_budget:
        status_name = "green"
    elif projected_closing <= available_budget * Decimal("1.10"):
        status_name = "yellow"
    else:
        status_name = "red"

    for day_number in range(1, total_days + 1):
        day_totals = day_map.get(day_number, {"income": Decimal("0"), "expense": Decimal("0")})
        income = round_money(day_totals["income"])
        spent = round_money(day_totals["expense"])
        net = round_money(income - spent)
        remaining = round_money(target_daily_goal - spent)
        progress = float(min(Decimal("100"), (spent / target_daily_goal) * Decimal("100"))) if target_daily_goal > 0 else 0.0
        day_status = "over" if spent > target_daily_goal else ("empty" if spent == 0 else "ok")
        days.append(
            {
                "day": day_number,
                "spent": spent,
                "income": income,
                "expense": spent,
                "net": net,
                "dailyGoalDelta": remaining,
                "remaining": remaining,
                "progress": progress,
                "status": day_status,
            }
        )

    days_above_goal = len([day for day in days if to_decimal(day["spent"]) > target_daily_goal])
    days_below_goal = len([day for day in days if Decimal("0") < to_decimal(day["spent"]) <= target_daily_goal])
    risk_alert = {
        "green": "Seu mês está dentro do orçamento planejado.",
        "yellow": "A projeção está até 10% acima do orçamento.",
        "red": "A projeção passa de 10% acima do orçamento.",
    }[status_name]

    return {
        "month": month,
        "dailyGoal": legacy_daily_goal,
        "reserveAmount": reserve_amount,
        "monthlyBudget": available_budget,
        "availableBudget": available_budget,
        "recommendedDailyGoal": recommended_daily_goal,
        "targetDailyGoal": target_daily_goal,
        "allowedRemaining": allowed_remaining,
        "daysAboveGoal": days_above_goal,
        "daysBelowGoal": days_below_goal,
        "currentAverageSpend": current_average_spend,
        "projectedClosing": projected_closing,
        "goalStatus": status_name,
        "riskAlert": risk_alert,
        "totalOutflow": outflow,
        "outflowToToday": outflow_to_today,
        "progressDay": progress_day,
        "totalDays": total_days,
        "days": days,
    }


def get_budget_status(spent: Decimal, planned: Decimal) -> str:
    if planned <= 0:
        return "ok"
    if spent >= planned:
        return "over"
    if spent >= planned * Decimal("0.80"):
        return "attention"
    return "ok"


def get_budget_summary(user_id: str, month: str) -> dict:
    return request_cached(("budget", user_id, month), lambda: _compute_budget_summary(user_id, month))


def _compute_budget_summary(user_id: str, month: str) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              b.id,
              b.category_id,
              c.name AS category_name,
              c.color AS category_color,
              c.icon AS category_icon,
              b.planned_amount,
              COALESCE(SUM(t.amount), 0) AS spent
            FROM budgets b
            JOIN categories c ON c.id = b.category_id AND c.user_id = b.user_id
            LEFT JOIN transactions t
              ON t.user_id = b.user_id
             AND t.category_id = b.category_id
             AND t.type = 'expense'
             AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = b.month
            WHERE b.user_id = %s
              AND b.month = %s
              AND COALESCE(c.is_active, TRUE) = TRUE
            GROUP BY b.id, b.category_id, c.name, c.color, c.icon, b.planned_amount
            ORDER BY c.name ASC
            """,
            (user_id, month_key),
        )
        rows = normalize_rows(cursor.fetchall())

        cursor.execute(
            """
            SELECT c.id, c.name, c.color, c.icon, COALESCE(SUM(t.amount), 0) AS spent
            FROM categories c
            LEFT JOIN transactions t
              ON t.user_id = c.user_id
             AND t.category_id = c.id
             AND t.type = 'expense'
             AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            WHERE c.user_id = %s
              AND c.type = 'expense'
              AND COALESCE(c.is_active, TRUE) = TRUE
              AND NOT EXISTS (
                SELECT 1
                FROM budgets b
                WHERE b.user_id = c.user_id AND b.category_id = c.id AND b.month = %s
              )
            GROUP BY c.id, c.name, c.color, c.icon
            ORDER BY spent DESC, c.name ASC
            """,
            (month_key, user_id, month_key),
        )
        unbudgeted = normalize_rows(cursor.fetchall())

    items: list[dict] = []
    total_planned = Decimal("0")
    total_spent = Decimal("0")
    for row in rows:
        planned = round_money(row["planned_amount"])
        spent = round_money(row["spent"])
        total_planned += planned
        total_spent += spent
        progress = float(min(Decimal("100"), (spent / planned) * Decimal("100"))) if planned > 0 else 0.0
        status_name = get_budget_status(spent, planned)
        items.append(
            {
                "id": row["id"],
                "categoryId": row["category_id"],
                "categoryName": row["category_name"],
                "categoryColor": row["category_color"],
                "categoryIcon": row["category_icon"],
                "plannedAmount": planned,
                "spent": spent,
                "remaining": round_money(planned - spent),
                "progress": progress,
                "status": status_name,
            }
        )

    return {
        "month": month_key,
        "totalPlanned": round_money(total_planned),
        "totalSpent": round_money(total_spent),
        "remaining": round_money(total_planned - total_spent),
        "items": items,
        "unbudgetedCategories": [
            {
                "categoryId": row["id"],
                "categoryName": row["name"],
                "categoryColor": row["color"],
                "categoryIcon": row["icon"],
                "spent": round_money(row["spent"]),
            }
            for row in unbudgeted
        ],
    }


def list_categorization_rules(user_id: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT r.id, r.category_id, r.payment_method, r.pattern, c.name AS category_name
            FROM categorization_rules r
            JOIN categories c ON c.id = r.category_id AND c.user_id = r.user_id
            WHERE r.user_id = %s
            ORDER BY length(r.pattern) DESC, r.created_at ASC
            """,
            (user_id,),
        )
        return normalize_rows(cursor.fetchall())


def find_matching_rule(rules: list[dict], description: str) -> dict | None:
    normalized_description = normalize_duplicate_text(description)
    for rule in rules:
        if normalize_duplicate_text(rule["pattern"]) in normalized_description:
            return rule
    return None


def match_categorization_rule(user_id: str, description: str) -> dict | None:
    return find_matching_rule(list_categorization_rules(user_id), description)


def get_reports_summary(user_id: str, month: str) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    dashboard = get_dashboard(user_id, month_key)
    budget = get_budget_summary(user_id, month_key)
    cards = get_cards_summary(user_id, month_key)
    score_data = calculate_score(user_id, month_key)
    goals = get_goals(user_id, month_key)
    category_growth = get_category_growth(user_id, month_key)
    alerts = get_alerts_for_month(user_id, month_key)
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT payment_method, type, COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            GROUP BY payment_method, type
            ORDER BY total DESC
            """,
            (user_id, month_key),
        )
        payment_methods = normalize_rows(cursor.fetchall())
    return {
        "month": month_key,
        "dashboard": dashboard,
        "budget": budget,
        "cards": cards,
        "score": score_data,
        "goals": goals,
        "paymentMethods": payment_methods,
        "categoryGrowth": category_growth,
        "alerts": alerts,
    }


def get_category_growth(user_id: str, month: str) -> dict:
    previous_month = add_months(month, -1)
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              COALESCE(c.name, 'Sem categoria') AS name,
              COALESCE(c.color, '#14B8A6') AS color,
              COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND t.type = 'expense'
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            GROUP BY c.name, c.color
            """,
            (user_id, month),
        )
        current_rows = normalize_rows(cursor.fetchall())

        cursor.execute(
            """
            SELECT
              COALESCE(c.name, 'Sem categoria') AS name,
              COALESCE(c.color, '#14B8A6') AS color,
              COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND t.type = 'expense'
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            GROUP BY c.name, c.color
            """,
            (user_id, previous_month),
        )
        previous_rows = normalize_rows(cursor.fetchall())

    current_by_name = {row["name"]: row for row in current_rows}
    previous_by_name = {row["name"]: row for row in previous_rows}
    names = sorted(set(current_by_name) | set(previous_by_name))
    items = []
    for name in names:
        current_total = round_money(current_by_name.get(name, {}).get("total") or 0)
        previous_total = round_money(previous_by_name.get(name, {}).get("total") or 0)
        delta = round_money(current_total - previous_total)
        percent_change = None
        if previous_total > 0:
            percent_change = round_money((delta / previous_total) * Decimal("100"))
        color = current_by_name.get(name, previous_by_name.get(name, {})).get("color") or "#14B8A6"
        items.append(
            {
                "name": name,
                "color": color,
                "currentTotal": current_total,
                "previousTotal": previous_total,
                "delta": delta,
                "percentChange": percent_change,
            }
        )

    items.sort(key=lambda item: abs(to_decimal(item["delta"])), reverse=True)
    return {
        "month": month,
        "previousMonth": previous_month,
        "hasHistory": any(round_money(row.get("total") or 0) > 0 for row in previous_rows),
        "items": items,
    }


def payment_method_label(value: Any) -> str:
    labels = {
        "boleto": "Boleto",
        "cash": "Dinheiro",
        "credito": "Crédito",
        "credit": "Crédito",
        "debito": "Débito",
        "debit": "Débito",
        "dinheiro": "Dinheiro",
        "pix": "Pix",
        "transfer": "Transferência",
    }
    text = str(value or "").strip()
    return labels.get(text, text or "Outro")


def transaction_source_label(value: Any) -> str:
    labels = {
        "manual": "Manual",
        "csv_import": "Importação CSV",
        "open_finance_future": "Open Finance",
    }
    text = str(value or "").strip()
    return labels.get(text, text or "Manual")


def get_month_totals(user_id: str, month: str) -> dict:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              COALESCE(SUM(CASE WHEN type = 'income' THEN amount END), 0) AS inflow,
              COALESCE(SUM(CASE WHEN type = 'expense' THEN amount END), 0) AS outflow
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            """,
            (user_id, month),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Totais do m\u00eas n\u00e3o encontrados.")
    return {"inflow": round_money(row["inflow"]), "outflow": round_money(row["outflow"])}


def get_score_label(score: int) -> dict:
    if score <= 300:
        return {"label": "Cr\u00edtico", "color": "#eb4d43"}
    if score <= 500:
        return {"label": "Regular", "color": "#ff9800"}
    if score <= 700:
        return {"label": "Razo\u00e1vel", "color": "#ffd54f"}
    if score <= 850:
        return {"label": "Bom", "color": "#9be768"}
    return {"label": "Excelente", "color": "#2f7d32"}


def calculate_score(user_id: str, month: str) -> dict:
    return request_cached(("score", user_id, month), lambda: _compute_score(user_id, month))


def _compute_score(user_id: str, month: str) -> dict:
    user_settings = get_settings(user_id)
    monthly_income = round_money(user_settings["monthly_income"] or 0)
    totals = get_month_totals(user_id, month)
    inflow = totals["inflow"]
    outflow = totals["outflow"]
    base = 1000
    breakdown = {"gastos": 0, "consistência": 0, "reservas": 0, "cartões": 0, "orçamento": 0}

    # DOM-05: renda efetiva (o maior entre configurada e o que já entrou),
    # não a soma das duas — ver get_effective_income.
    denominator = get_effective_income(user_settings, inflow)
    ratio_gastos = (outflow / denominator) if denominator > 0 else (Decimal("1") if outflow > 0 else Decimal("0"))
    if ratio_gastos > Decimal("0.9"):
        breakdown["gastos"] = -200
    elif ratio_gastos > Decimal("0.75"):
        breakdown["gastos"] = -120
    elif ratio_gastos > Decimal("0.6"):
        breakdown["gastos"] = -60
    elif ratio_gastos > Decimal("0.4"):
        breakdown["gastos"] = -20

    recent_months = [add_months(month, offset) for offset in (-2, -1, 0)]
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT COUNT(*) AS total
            FROM transactions
            WHERE user_id = %s
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
            """,
            (user_id, recent_months),
        )
        consistency_row = require_row(normalize_row(cursor.fetchone()), "Consist\u00eancia n\u00e3o encontrada.")
        total_recent = int(consistency_row["total"])
        if total_recent >= 20:
            breakdown["consistência"] = 50
        elif total_recent >= 10:
            breakdown["consistência"] = 25

        cursor.execute(
            """
            SELECT COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND lower(c.name) IN ('reserva', 'investimentos')
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            """,
            (user_id, month),
        )
        reserve_row = require_row(normalize_row(cursor.fetchone()), "Reservas n\u00e3o encontradas.")

    total_reserva = round_money(reserve_row["total"] or 0)
    if total_reserva > 0 and monthly_income > 0:
        breakdown["reservas"] = min(100, int((total_reserva / monthly_income) * Decimal("200")))

    invoice_totals = get_invoice_totals_by_card(user_id, month)
    for card in list_cards(user_id):
        credit_limit = round_money(card["credit_limit"] or 0)
        if credit_limit <= 0:
            continue
        uso_pct = invoice_totals.get(int(card["id"]), Decimal("0")) / credit_limit
        if uso_pct > Decimal("0.9"):
            breakdown["cartões"] -= 80
        elif uso_pct > Decimal("0.7"):
            breakdown["cartões"] -= 40

    budget = get_budget_summary(user_id, month)
    over_budget = len([item for item in budget["items"] if item["status"] == "over"])
    attention_budget = len([item for item in budget["items"] if item["status"] == "attention"])
    if over_budget:
        breakdown["orçamento"] -= min(120, over_budget * 40)
    elif budget["items"] and not attention_budget:
        breakdown["orçamento"] += 50

    base += sum(breakdown.values())
    score = max(0, min(1000, int(base)))
    label = get_score_label(score)
    return {"score": score, "label": label["label"], "color": label["color"], "breakdown": breakdown}


def get_alerts_for_month(user_id: str, month: str) -> list[dict]:
    user_settings = get_settings(user_id)
    totals = get_month_totals(user_id, month)
    alerts: list[dict] = []

    invoice_totals = get_invoice_totals_by_card(user_id, month)
    for card in list_cards(user_id):
        credit_limit = round_money(card["credit_limit"] or 0)
        if credit_limit <= 0:
            continue
        invoice = invoice_totals.get(int(card["id"]), Decimal("0"))
        usage = invoice / credit_limit
        if usage > Decimal("0.8"):
            usage_percent = int((usage * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))
            alerts.append(
                {
                    "type": "danger",
                    "category": "cart\u00e3o",
                    "message": f"Cart\u00e3o {card['name']} est\u00e1 com {usage_percent}% do limite",
                }
            )

    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT c.id, c.name, COALESCE(SUM(t.amount), 0) AS total
            FROM transactions t
            JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            WHERE t.user_id = %s
              AND t.type = 'expense'
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            GROUP BY c.id, c.name
            HAVING COALESCE(SUM(t.amount), 0) > 0
            """,
            (user_id, month),
        )
        current_categories = normalize_rows(cursor.fetchall())
        previous_months = [add_months(month, offset) for offset in (-3, -2, -1)]
        # M\u00e9dia dos 3 meses anteriores de todas as categorias em uma query s\u00f3,
        # em vez de uma por categoria.
        category_ids = [int(category["id"]) for category in current_categories]
        previous_by_category: dict[int, Decimal] = {}
        if category_ids:
            cursor.execute(
                """
                SELECT category_id, COALESCE(SUM(amount), 0) AS total
                FROM transactions
                WHERE user_id = %s
                  AND type = 'expense'
                  AND category_id = ANY(%s)
                  AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
                GROUP BY category_id
                """,
                (user_id, category_ids, previous_months),
            )
            previous_by_category = {
                int(row["category_id"]): to_decimal(row["total"] or 0)
                for row in normalize_rows(cursor.fetchall())
            }

        for category in current_categories:
            previous_total = previous_by_category.get(int(category["id"]), Decimal("0"))
            average = round_money(previous_total / Decimal("3"))
            current_total = round_money(category["total"] or 0)
            if average > 0 and current_total > average * Decimal("1.3"):
                percent = int((((current_total / average) - 1) * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))
                alerts.append(
                    {
                        "type": "warning",
                        "category": "gastos",
                        "message": f"Gastos com {category['name']} {percent}% acima da m\u00e9dia",
                    }
                )

        next_month = add_months(month, 1)
        cursor.execute(
            """
            SELECT COALESCE(SUM(amount), 0) AS total
            FROM transactions
            WHERE user_id = %s
              AND type = 'expense'
              AND installment_group IS NOT NULL
              AND billing_month = %s
            """,
            (user_id, next_month),
        )
        next_invoice_row = require_row(normalize_row(cursor.fetchone()), "Fatura estimada n\u00e3o encontrada.")

    if totals["inflow"] <= 0:
        alerts.append(
            {
                "type": "warning",
                "category": "gastos",
                "message": f"Nenhuma entrada lan\u00e7ada para {format_month_label(month)}",
            }
        )

    # DOM-05: renda efetiva (o maior entre configurada e o que já entrou),
    # não a soma das duas — ver get_effective_income.
    projected_balance = get_effective_income(user_settings, totals["inflow"]) - totals["outflow"]
    if projected_balance < 0:
        alerts.append(
            {
                "type": "danger",
                "category": "gastos",
                "message": f"Saldo projetado negativo em {format_brl(abs(projected_balance))}",
            }
        )

    next_invoice = round_money(next_invoice_row["total"] or 0)
    if next_invoice > Decimal("500"):
        alerts.append(
            {
                "type": "info",
                "category": "cart\u00e3o",
                "message": f"Fatura estimada em {format_brl(next_invoice)} para o pr\u00f3ximo m\u00eas",
            }
        )

    goals = get_goals(user_id, month)
    days = goals["days"]
    goal_reference = to_decimal(goals.get("targetDailyGoal") or goals["dailyGoal"])
    exceeded_days = [day for day in days if to_decimal(day["spent"]) > goal_reference]
    if days and len(exceeded_days) / len(days) > 0.5:
        alerts.append(
            {
                "type": "warning",
                "category": "meta",
                "message": "Meta di\u00e1ria estourada em mais da metade dos dias",
            }
        )
    if goals.get("goalStatus") in {"yellow", "red"}:
        alerts.append(
            {
                "type": "warning" if goals["goalStatus"] == "yellow" else "danger",
                "category": "meta",
                "message": goals["riskAlert"],
            }
        )

    budget = get_budget_summary(user_id, month)
    for item in budget["items"]:
        if item["status"] == "over":
            alerts.append(
                {
                    "type": "danger",
                    "category": "orcamento",
                    "message": f"{item['categoryName']} passou do orçamento em {format_brl(abs(item['remaining']))}",
                }
            )
        elif item["status"] == "attention":
            alerts.append(
                {
                    "type": "warning",
                    "category": "orcamento",
                    "message": f"{item['categoryName']} está perto do limite planejado.",
                }
            )

    return alerts


def normalize_recurrence(
    is_recurring: bool,
    recurrence_type: str | None,
    recurrence_day: int | None,
    transaction_date: str | None = None,
) -> tuple[bool, str | None, int | None]:
    if not is_recurring:
        return False, None, None

    if recurrence_type not in ("monthly", "weekly"):
        raise HTTPException(status_code=400, detail="Tipo de recorr\u00eancia inv\u00e1lido.")

    if recurrence_day is None and transaction_date:
        parsed = datetime.strptime(transaction_date, "%Y-%m-%d").date()
        recurrence_day = parsed.day if recurrence_type == "monthly" else parsed.weekday()

    if recurrence_day is None:
        raise HTTPException(status_code=400, detail="Dia da recorr\u00eancia \u00e9 obrigat\u00f3rio.")

    if recurrence_type == "monthly" and not 1 <= recurrence_day <= 31:
        raise HTTPException(status_code=400, detail="Dia mensal deve ficar entre 1 e 31.")
    if recurrence_type == "weekly" and not 0 <= recurrence_day <= 6:
        raise HTTPException(status_code=400, detail="Dia semanal deve ficar entre 0 e 6.")

    return True, recurrence_type, recurrence_day


def suggested_dates_for_recurrence(month: str, recurrence_type: str, recurrence_day: int) -> list[str]:
    from calendar import monthrange

    year, month_num = [int(part) for part in month.split("-")]
    total_days = monthrange(year, month_num)[1]
    if recurrence_type == "monthly":
        return [date(year, month_num, min(recurrence_day, total_days)).isoformat()]

    dates: list[str] = []
    for day in range(1, total_days + 1):
        current = date(year, month_num, day)
        if current.weekday() == recurrence_day:
            dates.append(current.isoformat())
    return dates


def get_recurring_suggestions(user_id: str, month: str) -> list[dict]:
    previous_month = add_months(month, -1)
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT id, title, amount, type, category_id, payment_method, notes, card_id,
                   is_recurring, recurrence_type, recurrence_day
            FROM transactions
            WHERE user_id = %s
              AND is_recurring = TRUE
              AND recurrence_type IS NOT NULL
              AND recurrence_day IS NOT NULL
              AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = %s
            ORDER BY transaction_date ASC, id ASC
            """,
            (user_id, previous_month),
        )
        recurring_rows = normalize_rows(cursor.fetchall())

        suggestions: list[dict] = []
        seen: set[tuple[Any, ...]] = set()
        for row in recurring_rows:
            dates = suggested_dates_for_recurrence(month, row["recurrence_type"], int(row["recurrence_day"]))
            for suggested_date in dates:
                key = (row["title"], round_money(row["amount"]), row["category_id"], suggested_date)
                if key in seen:
                    continue
                seen.add(key)

                cursor.execute(
                    """
                    SELECT 1
                    FROM transactions
                    WHERE user_id = %s
                      AND title = %s
                      AND amount = %s
                      AND COALESCE(category_id, 0) = COALESCE(%s, 0)
                      AND transaction_date = %s
                    LIMIT 1
                    """,
                    (user_id, row["title"], row["amount"], row["category_id"], suggested_date),
                )
                if cursor.fetchone():
                    continue

                suggestions.append(
                    {
                        "title": row["title"],
                        "amount": round_money(row["amount"]),
                        "type": row["type"],
                        "category_id": row["category_id"],
                        "payment_method": row["payment_method"],
                        "card_id": row["card_id"],
                        "suggested_date": suggested_date,
                        "notes": row.get("notes") or "",
                        "is_recurring": True,
                        "recurrence_type": row["recurrence_type"],
                        "recurrence_day": row["recurrence_day"],
                    }
                )

    return suggestions


class RegisterPayload(BaseModel):
    email: str = Field(..., min_length=3, max_length=255)
    password: str = Field(..., min_length=8, max_length=72)
    name: str = Field(..., min_length=1, max_length=100)
    accept_terms: bool = False

    class Config:
        extra = "forbid"


class DeleteAccountPayload(BaseModel):
    password: str | None = None

    class Config:
        extra = "forbid"


class ConsentPayload(BaseModel):
    scope: str = Field(..., min_length=1, max_length=50)
    granted: bool

    class Config:
        extra = "forbid"


class SettingsPayload(BaseModel):
    monthlyIncome: Decimal | None = Field(default=None, ge=0, le=999999999)
    dailyGoal: Decimal | None = Field(default=None, ge=0, le=999999999)
    reserveAmount: Decimal | None = Field(default=None, ge=0, le=999999999)
    reserveGoalAmount: Decimal | None = Field(default=None, ge=0, le=999999999)
    reserveCurrentAmount: Decimal | None = Field(default=None, ge=0, le=999999999)

    class Config:
        extra = "forbid"


class CategoryPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=80)
    type: Literal["income", "expense"] = "expense"
    color: str = Field(default="#9be768", min_length=1, max_length=20)
    icon: str = Field(default="\u25cf", min_length=1, max_length=10)

    class Config:
        extra = "forbid"


class TransactionPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    amount: Decimal = Field(..., gt=0, le=999999999)
    type: Literal["income", "expense"] = "expense"
    categoryId: int | None = Field(default=None, ge=1)
    paymentMethod: str = Field(default="pix", min_length=1, max_length=50)
    transactionDate: str = Field(..., min_length=10, max_length=10)
    notes: str = Field(default="", max_length=1000)
    cardId: int | None = Field(default=None, ge=1)
    billingMonth: str | None = Field(default=None, min_length=7, max_length=7)
    isRecurring: bool = False
    recurrenceType: Literal["monthly", "weekly"] | None = None
    recurrenceDay: int | None = Field(default=None, ge=0, le=31)

    class Config:
        extra = "forbid"


class CardPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    brand: str = Field(..., min_length=1, max_length=40)
    lastFour: str = Field(..., min_length=4, max_length=4)
    creditLimit: Decimal = Field(..., ge=0, le=999999999)
    closingDay: int = Field(..., ge=1, le=31)
    dueDay: int = Field(..., ge=1, le=31)
    color: str = Field(default="#171717", min_length=1, max_length=20)

    class Config:
        extra = "forbid"


class InstallmentPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    categoryId: int | None = Field(default=None, ge=1)
    totalAmount: Decimal = Field(..., gt=0, le=999999999)
    totalInstallments: int = Field(..., ge=2, le=24)
    purchaseDate: str = Field(..., min_length=10, max_length=10)
    notes: str = Field(default="", max_length=1000)

    class Config:
        extra = "forbid"


class PurchaseSimulationPayload(BaseModel):
    totalAmount: Decimal = Field(..., gt=0, le=999999999)
    totalInstallments: int = Field(..., ge=2, le=24)
    purchaseDate: str = Field(..., min_length=10, max_length=10)
    months: int = Field(default=12, ge=1, le=24)

    class Config:
        extra = "forbid"


class InstallmentSimulationPayload(BaseModel):
    totalAmount: Decimal = Field(..., gt=0, le=999999999)
    totalInstallments: int = Field(..., ge=2, le=24)
    interestRate: float = Field(default=0, ge=0, le=100)
    purchaseDate: str = Field(..., min_length=10, max_length=10)
    months: int = Field(default=12, ge=1, le=24)

    class Config:
        extra = "forbid"


class InstallmentWithoutCardPayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    categoryId: int | None = Field(default=None, ge=1)
    totalAmount: Decimal = Field(..., gt=0, le=999999999)
    totalInstallments: int = Field(..., ge=2, le=24)
    interestRate: float = Field(default=0, ge=0, le=100)
    purchaseDate: str = Field(..., min_length=10, max_length=10)
    notes: str = Field(default="", max_length=1000)

    class Config:
        extra = "forbid"


class CsvColumnMapping(BaseModel):
    date: str = Field(..., min_length=1, max_length=120)
    description: str = Field(..., min_length=1, max_length=120)
    value: str = Field(..., min_length=1, max_length=120)
    type: str | None = Field(default=None, max_length=120)
    # Opcionais: quando o arquivo traz a categoria e a conta/origem, elas são
    # aproveitadas em vez de todo lançamento cair em "sem categoria".
    category: str | None = Field(default=None, max_length=120)
    account: str | None = Field(default=None, max_length=120)
    time: str | None = Field(default=None, max_length=120)

    class Config:
        extra = "forbid"


class CsvImportPreviewPayload(BaseModel):
    importToken: str = Field(..., min_length=16, max_length=200)
    mapping: CsvColumnMapping

    class Config:
        extra = "forbid"


class CsvImportConfirmPayload(CsvImportPreviewPayload):
    # "merge" mantém o que já existe e ignora duplicatas; "replace" troca os
    # lançamentos dos meses presentes no arquivo. O default é o modo seguro.
    mode: Literal["merge", "replace"] = "merge"


class PinPayload(BaseModel):
    pin: str = Field(..., min_length=4, max_length=6)

    class Config:
        extra = "forbid"


class ProfilePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    avatar_url: str | None = Field(default=None, max_length=500)
    send_monthly_summary: bool | None = None

    class Config:
        extra = "forbid"


class ChangePasswordPayload(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=72)
    new_password: str = Field(..., min_length=8, max_length=72)

    class Config:
        extra = "forbid"


class RecurringPayload(BaseModel):
    is_recurring: bool
    recurrence_type: Literal["monthly", "weekly"] | None = None
    recurrence_day: int | None = Field(default=None, ge=0, le=31)

    class Config:
        extra = "forbid"


class TransactionUpdatePayload(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    amount: Decimal | None = Field(default=None, gt=0, le=999999999)
    type: Literal["income", "expense"] | None = None
    categoryId: int | None = Field(default=None, ge=1)
    paymentMethod: str | None = Field(default=None, min_length=1, max_length=50)
    transactionDate: str | None = Field(default=None, min_length=10, max_length=10)
    notes: str | None = Field(default=None, max_length=1000)
    cardId: int | None = Field(default=None, ge=1)
    billingMonth: str | None = Field(default=None, min_length=7, max_length=7)

    class Config:
        extra = "forbid"


class BudgetPayload(BaseModel):
    categoryId: int = Field(..., ge=1)
    month: str = Field(..., min_length=7, max_length=7)
    plannedAmount: Decimal = Field(..., ge=0, le=999999999)

    class Config:
        extra = "forbid"


class BudgetCopyPayload(BaseModel):
    fromMonth: str = Field(..., min_length=7, max_length=7)
    toMonth: str = Field(..., min_length=7, max_length=7)

    class Config:
        extra = "forbid"


class CategorizationRulePayload(BaseModel):
    pattern: str = Field(..., min_length=2, max_length=120)
    categoryId: int = Field(..., ge=1)
    paymentMethod: str | None = Field(default=None, min_length=1, max_length=50)

    class Config:
        extra = "forbid"


class CardUpdatePayload(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    brand: str | None = Field(default=None, min_length=1, max_length=40)
    lastFour: str | None = Field(default=None, min_length=4, max_length=4)
    creditLimit: Decimal | None = Field(default=None, ge=0, le=999999999)
    closingDay: int | None = Field(default=None, ge=1, le=31)
    dueDay: int | None = Field(default=None, ge=1, le=31)
    color: str | None = Field(default=None, min_length=1, max_length=20)

    class Config:
        extra = "forbid"


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


@app.get("/api/health/live")
def liveness():
    # Liveness: process is up. No dependencies checked (no DB), so it stays green
    # during transient database blips — used to decide restarts, not readiness.
    return {"ok": True, "status": "alive", "uptime_seconds": int(time.time() - startup_time)}


@app.get("/api/health")
def health():
    started_at = time.perf_counter()
    try:
        with db_cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        latency_ms = round((time.perf_counter() - started_at) * 1000, 2)
        return {
            "ok": True,
            "db": "connected",
            "version": "2.0.0",
            "uptime_seconds": int(time.time() - startup_time),
            "checks": {
                "database": {"status": "ok", "latency_ms": latency_ms},
                "migrations": {"status": "ok"},
                # SEC-05: 'database' significa que JWT_SECRET_KEY não está
                # definida e o servidor está assinando sessões com um
                # segredo gerado e guardado em app_secrets — nunca expõe o
                # valor, só a origem.
                "signing": {"source": secret_source()},
            },
        }
    except Exception:
        logger.exception("Health check failed")
        return JSONResponse(
            {
                "ok": False,
                "db": "error",
                "version": "2.0.0",
                "uptime_seconds": int(time.time() - startup_time),
                "checks": {
                    "database": {"status": "error", "latency_ms": None},
                    "migrations": {"status": "unknown"},
                    "signing": {"source": secret_source()},
                },
            },
            status_code=503,
        )


@app.post("/api/auth/register", status_code=status.HTTP_201_CREATED)
@limiter.limit("3 per 1 hour")
def register(request: Request, response: Response, payload: RegisterPayload) -> dict:
    enforce_ip_rate_limit(request, "register", max_attempts=3, window_seconds=3600)
    email = normalize_email(payload.email)
    name = clean_text(payload.name, "Nome", 100)
    validate_password_strength(payload.password)
    if not payload.accept_terms:
        raise HTTPException(status_code=400, detail="\u00c9 necess\u00e1rio aceitar a Pol\u00edtica de Privacidade e os Termos.")

    ip_hash = client_ip_hash(request)
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO users (email, hashed_password, name)
                VALUES (%s, %s, %s)
                RETURNING id, email, name, avatar_url, send_monthly_summary, is_active,
                          auth_provider, oauth_subject, password_changed_at, created_at, updated_at
                """,
                (email, hash_password(payload.password), name),
            )
            user = require_row(normalize_row(cursor.fetchone()), "Usu\u00e1rio n\u00e3o criado.")
            ensure_user_defaults_for_cursor(cursor, user["id"])
            record_consent(cursor, user["id"], scope="terms_privacy", granted=True, ip_hash=ip_hash, channel="register")
    except errors.UniqueViolation:
        audit_log("user_register_failed", None, {"email_hash": email_hash(email), "reason": "duplicate"})
        raise HTTPException(status_code=400, detail="E-mail j\u00e1 cadastrado.") from None

    audit_log("user_registered", str(user["id"]), {"email_hash": email_hash(email)})
    token = create_access_token(user["id"])
    set_auth_cookie(response, token)
    return token_response_body(request, token)


@app.get("/api/auth/oauth/providers")
def oauth_providers(request: Request) -> dict:
    set_request_origin(str(request.base_url))
    return {"providers": list_providers()}


@app.get("/api/auth/oauth/{provider}/authorize")
def oauth_authorize(
    provider: str,
    request: Request,
    link: bool = False,
    current_user: dict | None = Depends(get_optional_current_user),
) -> RedirectResponse:
    if provider not in OAUTH_PROVIDERS:
        raise HTTPException(status_code=404, detail="Provedor OAuth não suportado.")
    set_request_origin(str(request.base_url))
    link_user_id = None
    if link:
        # SEC-04: vincular uma conta social exige que o dono já esteja
        # autenticado por senha — sem isso, qualquer um poderia "vincular"
        # a própria conta social à conta de outra pessoa sem confirmação.
        if not current_user:
            raise HTTPException(
                status_code=401, detail="Entre com sua senha antes de vincular uma conta social."
            )
        link_user_id = str(current_user["id"])
    return build_authorize_redirect(provider, link_user_id=link_user_id)


@app.get("/api/auth/oauth/{provider}/callback")
def oauth_callback(
    request: Request,
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    oauth_state_cookie: str | None = Cookie(default=None, alias=OAUTH_STATE_COOKIE),
) -> RedirectResponse:
    set_request_origin(str(request.base_url))

    def redirect_and_clear(
        *, access_token: str | None = None, error_message: str | None = None, link_success: bool = False
    ) -> RedirectResponse:
        response = frontend_redirect(access_token=access_token, error=error_message, link_success=link_success)
        response.delete_cookie(OAUTH_STATE_COOKIE, path="/api/auth/oauth")
        return response

    if provider not in OAUTH_PROVIDERS:
        return redirect_and_clear(error_message="unsupported_provider")
    if error:
        logger.info("OAuth provider error provider=%s error=%s", provider, error)
        return redirect_and_clear(error_message=error_description or error)
    if not code or not state:
        return redirect_and_clear(error_message="missing_code")
    try:
        profile, state_payload = fetch_oauth_profile(provider, code, state, oauth_state_cookie)
        link_user_id = state_payload.get("link_user_id")
        if link_user_id:
            link_oauth_identity_to_user(link_user_id, profile)
            return redirect_and_clear(link_success=True)
        user = resolve_oauth_user(profile)
        audit_log("login_success_oauth", str(user["id"]), {"provider": provider, "email_hash": email_hash(user["email"])})
        token = create_access_token(user["id"])
        response = redirect_and_clear(access_token=token)
        set_auth_cookie(response, token)
        return response
    except HTTPException as exc:
        logger.info("OAuth callback failed provider=%s detail=%s", provider, exc.detail)
        return redirect_and_clear(error_message=str(exc.detail))
    except Exception:
        logger.exception("OAuth callback unexpected failure provider=%s", provider)
        return redirect_and_clear(error_message="oauth_failed")


@app.post("/api/auth/login")
@limiter.limit("5 per 15 minutes")
def login(
    request: Request,
    response: Response,
    email: str = Form(..., max_length=255),
    password: str = Form(..., max_length=72),
) -> dict:
    email_value = email.strip().lower()
    enforce_login_rate_limit(email_value)
    email_is_valid = len(email_value) <= 255 and bool(EMAIL_RE.match(email_value))
    user = get_user_by_email(email_value) if email_is_valid else None
    stored_hash = user["hashed_password"] if user else None
    hash_to_check = stored_hash if stored_hash else DUMMY_PASSWORD_HASH
    password_ok = verify_password(password, hash_to_check)

    if not user or not password_ok or not user["is_active"]:
        record_login_failure(email_value)
        audit_log("login_failed", str(user["id"]) if user else None, {"email_hash": email_hash(email_value)})
        raise HTTPException(status_code=401, detail="E-mail ou senha inv\u00e1lidos.")

    clear_login_failures(email_value)
    audit_log("login_success", str(user["id"]), {"email_hash": email_hash(email_value)})
    token = create_access_token(user["id"])
    set_auth_cookie(response, token)
    return token_response_body(request, token)


@app.get("/api/auth/me")
def me(current_user: dict = Depends(get_current_user)) -> dict:
    return current_user


@app.get("/api/auth/csrf")
def get_csrf(response: Response, current_user: dict = Depends(get_current_user)) -> dict:
    token = issue_csrf_cookie(response)
    return {"csrf_token": token}


@app.put("/api/auth/me")
def update_me(payload: ProfilePayload, current_user: dict = Depends(get_current_user)) -> dict:
    return save_profile_payload(payload, current_user)


@app.post("/api/auth/me")
def save_me(payload: ProfilePayload, current_user: dict = Depends(get_current_user)) -> dict:
    return save_profile_payload(payload, current_user)


def save_profile_payload(payload: ProfilePayload, current_user: dict) -> dict:
    user_id = current_user["id"]
    fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    name = current_user["name"]
    previous_avatar_url = current_user.get("avatar_url")
    avatar_url = previous_avatar_url
    send_monthly_summary = bool(current_user.get("send_monthly_summary", False))

    if "name" in fields_set:
        if payload.name is None:
            raise HTTPException(status_code=400, detail="Nome \u00e9 obrigat\u00f3rio.")
        name = clean_text(payload.name, "Nome", 100)

    if "avatar_url" in fields_set:
        avatar_url = None
    if "avatar_url" in fields_set and payload.avatar_url is not None:
        avatar_url = validate_optional_url(payload.avatar_url, "URL do avatar")

    if "send_monthly_summary" in fields_set and payload.send_monthly_summary is not None:
        send_monthly_summary = bool(payload.send_monthly_summary)

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE users
            SET name = %s, avatar_url = %s, send_monthly_summary = %s
            WHERE id = %s
            RETURNING id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                      auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            """,
            (name, avatar_url, send_monthly_summary, user_id),
        )
        user = require_row(normalize_row(cursor.fetchone()), "Usu\u00e1rio n\u00e3o atualizado.")
    if "avatar_url" in fields_set and avatar_url is None:
        delete_profile_photo_file(previous_avatar_url)
    return public_user(user)


@app.post("/api/auth/me/avatar")
@limiter.limit("12 per 1 hour")
async def upload_profile_photo(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
) -> dict:
    enforce_ip_rate_limit(request, "avatar_upload", max_attempts=12, window_seconds=3600)
    content = await file.read(PROFILE_PHOTO_MAX_BYTES + 1)
    if not content:
        raise HTTPException(status_code=400, detail="Escolha uma imagem para enviar.")
    if len(content) > PROFILE_PHOTO_MAX_BYTES:
        raise HTTPException(status_code=400, detail="A foto deve ter no máximo 512 KB.")

    extension = detect_profile_photo_extension(file.content_type, content)
    user_id = str(current_user["id"])
    normalized_type = (file.content_type or "").split(";")[0].strip().lower() or "application/octet-stream"
    try:
        avatar_url = storage.store_avatar(user_id, content, extension, normalized_type)
    except ValueError:
        raise HTTPException(status_code=400, detail="Nome de arquivo inválido.") from None
    previous_avatar_url = current_user.get("avatar_url")

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE users
            SET avatar_url = %s
            WHERE id = %s
            RETURNING id, email, hashed_password, name, avatar_url, send_monthly_summary, is_active,
                      auth_provider, oauth_subject, password_changed_at, created_at, updated_at
            """,
            (avatar_url, user_id),
        )
        user = require_row(normalize_row(cursor.fetchone()), "Foto de perfil não atualizada.")

    delete_profile_photo_file(previous_avatar_url)
    audit_log("profile_photo_uploaded", user_id)
    return public_user(user)


@app.post("/api/auth/change-password")
@limiter.limit("3 per 1 hour")
def change_password(
    request: Request,
    payload: ChangePasswordPayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    enforce_ip_rate_limit(request, "change_password", max_attempts=3, window_seconds=3600)
    user = get_user_by_id(current_user["id"])
    if not user or not user.get("hashed_password") or not verify_password(payload.current_password, user["hashed_password"]):
        raise HTTPException(status_code=400, detail="Senha atual incorreta.")

    validate_password_strength(payload.new_password)
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE users
            SET hashed_password = %s, password_changed_at = date_trunc('second', NOW())
            WHERE id = %s
            """,
            (hash_password(payload.new_password), current_user["id"]),
        )
    audit_log("password_changed", current_user["id"])
    return {"ok": True}


@app.get("/api/auth/stats")
def auth_stats(current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM transactions WHERE user_id = %s) AS total_transactions,
              (SELECT COUNT(*) FROM categories WHERE user_id = %s) AS total_categories
            """,
            (user_id, user_id),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Estat\u00edsticas n\u00e3o encontradas.")
    return {
        "created_at": current_user["created_at"],
        "total_transactions": int(row["total_transactions"]),
        "total_categories": int(row["total_categories"]),
    }


@app.post("/api/auth/logout")
def logout(
    response: Response,
    token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    current_user: dict = Depends(get_current_user),
) -> dict:
    token = token or cookie_token
    if not token:
        raise HTTPException(status_code=401, detail="Token inv\u00e1lido ou expirado.")
    revoke_token(token, current_user["id"])
    clear_auth_cookie(response)
    return {"message": "Sessão encerrada no servidor."}


@app.delete("/api/auth/me")
@limiter.limit("5 per 1 hour")
def delete_account(
    request: Request,
    response: Response,
    payload: DeleteAccountPayload,
    token: str | None = Depends(oauth2_scheme),
    cookie_token: str | None = Cookie(default=None, alias=AUTH_COOKIE_NAME),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """LGPD Art. 18, IV — direito de eliminação.

    Exige reautenticação por senha (contas com senha); as FKs ON DELETE CASCADE
    em user_id removem settings/categorias/transações/cartões/orçamentos/etc.
    """
    enforce_ip_rate_limit(request, "delete_account", max_attempts=5, window_seconds=3600)
    user = get_user_by_id(current_user["id"])
    if user and user.get("hashed_password") and (
        not payload.password or not verify_password(payload.password, user["hashed_password"])
    ):
        raise HTTPException(status_code=400, detail="Senha incorreta.")

    avatar_ref = user.get("avatar_url") if user else None
    deleted_user_id = current_user["id"]
    with db_cursor(commit=True) as cursor:
        cursor.execute("DELETE FROM users WHERE id = %s", (deleted_user_id,))

    if avatar_ref and not storage.remove_avatar(avatar_ref):
        # SEC-11: a conta já foi apagada; sem isto, a falha desaparecia e a
        # eliminação LGPD ficava incompleta sem ninguém saber. Best-effort
        # também aqui — se ATÉ o registro da pendência falhar, loga e segue
        # (a exclusão da conta em si não pode travar por causa do avatar).
        try:
            with db_cursor(commit=True) as cursor:
                cursor.execute(
                    "INSERT INTO pending_avatar_deletions (user_id, avatar_ref) VALUES (%s, %s)",
                    (deleted_user_id, avatar_ref),
                )
        except Exception:
            logger.exception("Failed to record pending avatar deletion for user_id=%s", deleted_user_id)
        audit_log("avatar_deletion_failed", deleted_user_id, {"avatar_ref": avatar_ref})

    active_token = token or cookie_token
    if active_token:
        revoke_token(active_token, current_user["id"])
    clear_auth_cookie(response)
    audit_log("account_deleted", current_user["id"])
    return {"deleted": True}


@app.get("/api/privacy/export")
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


@app.post("/api/privacy/consent")
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


@app.get("/api/bootstrap")
def bootstrap(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    month_key = validate_month_text(month) or get_current_month()
    ensure_user_defaults(user_id)
    score = calculate_score(user_id, month_key)
    previous_score = calculate_score(user_id, add_months(month_key, -1))
    return {
        "settings": get_settings(user_id),
        "categories": list_categories(user_id),
        "cards": get_cards_summary(user_id, month_key),
        "transactions": list_transactions(user_id, month_key),
        "dashboard": get_dashboard(user_id, month_key),
        "budget": get_budget_summary(user_id, month_key),
        "score": score,
        "previousScore": previous_score,
        "alerts": get_alerts_for_month(user_id, month_key),
        "recurringSuggestions": get_recurring_suggestions(user_id, month_key),
        "user": current_user,
    }


@app.get("/api/score")
def score(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    return calculate_score(current_user["id"], month_key)


@app.get("/api/alerts")
def alerts(month: str | None = None, current_user: dict = Depends(get_current_user)) -> list[dict]:
    month_key = validate_month_text(month) or get_current_month()
    return get_alerts_for_month(current_user["id"], month_key)


@app.get("/api/transactions/suggestions")
def transaction_suggestions(month: str | None = None, current_user: dict = Depends(get_current_user)) -> list[dict]:
    month_key = validate_month_text(month) or get_current_month()
    return get_recurring_suggestions(current_user["id"], month_key)


@app.get("/api/transactions")
def transactions(
    month: str | None = None,
    type: Literal["income", "expense"] | None = None,
    categoryId: int | None = Query(default=None, ge=1),
    paymentMethod: str | None = None,
    source: Literal["manual", "csv_import", "open_finance_future"] | None = None,
    cardId: int | None = Query(default=None, ge=1),
    search: str | None = Query(default=None, max_length=120),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    month_key = validate_month_text(month) if month else None
    payment_method = clean_text(paymentMethod, "Forma de pagamento", 50, required=False) if paymentMethod else None
    search_text = clean_text(search, "Busca", 120, required=False) if search else None
    return list_transactions(
        current_user["id"],
        month_key,
        type,
        categoryId,
        payment_method,
        source,
        cardId,
        search_text,
    )


@app.post("/api/imports/csv/upload")
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


@app.post("/api/imports/csv/preview")
def preview_csv_import(payload: CsvImportPreviewPayload, current_user: dict = Depends(get_current_user)) -> dict:
    session = get_csv_import_session(current_user["id"], payload.importToken)
    preview = build_csv_import_preview(current_user["id"], session, payload.mapping)
    preview.pop("rows", None)
    return preview


@app.post("/api/imports/csv/confirm")
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


@app.get("/api/goals")
def goals(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    return get_goals(current_user["id"], month_key)


@app.get("/api/budgets")
def budgets(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    return get_budget_summary(current_user["id"], month_key)


@app.post("/api/budgets")
def save_budget(payload: BudgetPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    month_key = validate_month_text(payload.month) or get_current_month()
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO budgets (user_id, category_id, month, planned_amount)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, category_id, month)
                DO UPDATE SET planned_amount = EXCLUDED.planned_amount, updated_at = NOW()
                RETURNING *
                """,
                (user_id, payload.categoryId, month_key, round_money(payload.plannedAmount)),
            )
            row = require_row(normalize_row(cursor.fetchone()), "Orçamento não salvo.")
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria inválida.") from None
    return row


@app.delete("/api/budgets/{budget_id}")
def delete_budget(budget_id: int, current_user: dict = Depends(get_current_user)) -> dict:
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            "DELETE FROM budgets WHERE user_id = %s AND id = %s RETURNING id",
            (current_user["id"], budget_id),
        )
        row = normalize_row(cursor.fetchone())
    if not row:
        raise HTTPException(status_code=404, detail="Or\u00e7amento n\u00e3o encontrado.")
    return {"deleted": True}


@app.post("/api/budgets/copy")
def copy_budget(payload: BudgetCopyPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    from_month = validate_month_text(payload.fromMonth) or get_current_month()
    to_month = validate_month_text(payload.toMonth) or get_current_month()
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            INSERT INTO budgets (user_id, category_id, month, planned_amount)
            SELECT b.user_id, b.category_id, %s, b.planned_amount
            FROM budgets b
            JOIN categories c ON c.id = b.category_id AND c.user_id = b.user_id
            WHERE b.user_id = %s
              AND b.month = %s
              AND COALESCE(c.is_active, TRUE) = TRUE
            ON CONFLICT (user_id, category_id, month)
            DO UPDATE SET planned_amount = EXCLUDED.planned_amount, updated_at = NOW()
            """,
            (to_month, user_id, from_month),
        )
    return get_budget_summary(user_id, to_month)


@app.get("/api/categorization-rules")
def categorization_rules(current_user: dict = Depends(get_current_user)) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT r.*, c.name AS category_name
            FROM categorization_rules r
            JOIN categories c ON c.id = r.category_id AND c.user_id = r.user_id
            WHERE r.user_id = %s
            ORDER BY r.created_at DESC
            """,
            (current_user["id"],),
        )
        return normalize_rows(cursor.fetchall())


@app.post("/api/categorization-rules")
def create_categorization_rule(
    payload: CategorizationRulePayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    pattern = normalize_duplicate_text(clean_text(payload.pattern, "Padrao", 120))
    payment_method = (
        clean_text(payload.paymentMethod, "Forma de pagamento", 50, required=False)
        if payload.paymentMethod
        else None
    )
    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO categorization_rules (user_id, pattern, category_id, payment_method)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id, pattern)
                DO UPDATE SET category_id = EXCLUDED.category_id, payment_method = EXCLUDED.payment_method
                RETURNING *
                """,
                (user_id, pattern, payload.categoryId, payment_method),
            )
            return require_row(normalize_row(cursor.fetchone()), "Regra não criada.")
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria inválida.") from None


@app.get("/api/reports")
def reports(month: str | None = None, current_user: dict = Depends(get_current_user)) -> dict:
    month_key = validate_month_text(month) or get_current_month()
    return get_reports_summary(current_user["id"], month_key)


@app.get("/api/cards")
def cards(month: str | None = None, current_user: dict = Depends(get_current_user)) -> list[dict]:
    month_key = validate_month_text(month) or get_current_month()
    return get_cards_summary(current_user["id"], month_key)


@app.get("/api/cards-detail")
def cards_detail(month: str | None = None, current_user: dict = Depends(get_current_user)) -> list[dict]:
    validate_month_text(month) if month else None
    user_id = current_user["id"]
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT c.id, c.name, c.brand, c.last_four,
                   EXISTS (
                     SELECT 1
                     FROM card_pins p
                     WHERE p.card_id = c.id AND p.user_id = c.user_id
                   ) AS has_pin
            FROM cards c
            WHERE c.user_id = %s
            ORDER BY c.created_at ASC, c.id ASC
            """,
            (user_id,),
        )
        rows = normalize_rows(cursor.fetchall())

    return [
        {
            "id": row["id"],
            "name": row["name"],
            "brand": row["brand"],
            "last_four": row["last_four"],
            "credit_limit": None,
            "invoice": None,
            "available_credit": None,
            "closing_day": None,
            "due_day": None,
            "has_pin": bool(row["has_pin"]),
            "is_unlocked": False,
        }
        for row in rows
    ]


@app.post("/api/cards/{card_id}/set-pin")
def set_card_pin(card_id: int, payload: PinPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    get_card_for_user(user_id, card_id)
    pin = validate_pin(payload.pin)

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            INSERT INTO card_pins (card_id, user_id, pin_hash)
            VALUES (%s, %s, %s)
            ON CONFLICT (card_id, user_id)
            DO UPDATE SET pin_hash = EXCLUDED.pin_hash, created_at = NOW()
            """,
            (card_id, user_id, hash_pin(pin)),
        )

    clear_card_pin_failures(user_id, card_id)
    invalidate_card_unlock_sessions(user_id, card_id)
    audit_log("card_pin_set", user_id, {"card_id": card_id})
    return {"ok": True}


@app.post("/api/cards/{card_id}/unlock")
def unlock_card(
    card_id: int,
    payload: PinPayload,
    month: str | None = None,
    categoryId: int | None = Query(default=None, ge=1),
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    month_key = validate_month_text(month) or get_current_month()
    get_card_for_user(user_id, card_id)
    pin = validate_pin(payload.pin)
    enforce_card_pin_rate_limit(user_id, card_id)

    pin_row = get_card_pin_row(user_id, card_id)
    if not pin_row:
        raise HTTPException(status_code=400, detail="PIN n\u00e3o definido.")

    if not verify_pin(pin, pin_row["pin_hash"]):
        attempts_remaining = record_card_pin_failure(user_id, card_id)
        audit_log("card_pin_wrong", user_id, {"card_id": card_id, "attempts_remaining": attempts_remaining})
        if attempts_remaining <= 0:
            audit_log("card_pin_blocked", user_id, {"card_id": card_id})
            raise HTTPException(
                status_code=429,
                detail="Muitas tentativas. Tente novamente em 5 minutos.",
                headers={"X-Attempts-Remaining": "0"},
            )
        raise HTTPException(
            status_code=401,
            detail="PIN incorreto",
            headers={"X-Attempts-Remaining": str(attempts_remaining)},
        )

    clear_card_pin_failures(user_id, card_id)
    audit_log("card_unlocked", user_id, {"card_id": card_id})
    return get_unlocked_card_details(user_id, card_id, month_key, include_token=True, category_id=categoryId)


@app.get("/api/cards/{card_id}/simulate-invoices")
def simulate_invoices_route(
    card_id: int,
    months: int = Query(12, ge=1, le=24),
    month: str | None = None,
    categoryId: int | None = Query(default=None, ge=1),
    x_card_unlock_token: str = Header(..., alias="X-Card-Unlock-Token"),
    current_user: dict = Depends(get_current_user),
) -> list[dict]:
    user_id = current_user["id"]
    month_key = validate_month_text(month) or get_current_month()
    get_card_for_user(user_id, card_id)
    verify_card_unlock_session(user_id, card_id, x_card_unlock_token)
    return simulate_card_invoices(user_id, card_id, month_key, months, categoryId)


@app.post("/api/cards/{card_id}/purchase-simulation")
def simulate_card_purchase(
    card_id: int,
    payload: PurchaseSimulationPayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    get_card_for_user(user_id, card_id)
    purchase_date = validate_date_text(payload.purchaseDate, "Data da compra")
    base_month = month_key_from_date(purchase_date)
    installment_amounts = distribute_installments(payload.totalAmount, payload.totalInstallments)
    current_invoices = simulate_card_invoices(user_id, card_id, base_month, payload.months)
    simulated_by_month = {
        add_months(base_month, index): amount for index, amount in enumerate(installment_amounts)
    }

    projection = []
    for invoice in current_invoices:
        simulated_amount = round_money(simulated_by_month.get(invoice["month"], Decimal("0")))
        projection.append(
            {
                "month": invoice["month"],
                "currentInvoice": invoice["projected_total"],
                "simulatedInstallment": simulated_amount,
                "projectedTotal": round_money(invoice["projected_total"] + simulated_amount),
            }
        )

    return {
        "cardId": card_id,
        "totalAmount": round_money(payload.totalAmount),
        "totalInstallments": payload.totalInstallments,
        "installments": installment_amounts,
        "projection": projection,
    }


@app.post("/api/settings")
def save_settings(payload: SettingsPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    current = get_settings(user_id)
    monthly_income = round_money(payload.monthlyIncome if payload.monthlyIncome is not None else current["monthly_income"])
    daily_goal = round_money(payload.dailyGoal if payload.dailyGoal is not None else current["daily_goal"])
    reserve_amount = round_money(payload.reserveAmount if payload.reserveAmount is not None else current["reserve_amount"])
    reserve_goal_amount = round_money(
        payload.reserveGoalAmount if payload.reserveGoalAmount is not None else current.get("reserve_goal_amount", 0)
    )
    reserve_current_amount = round_money(
        payload.reserveCurrentAmount if payload.reserveCurrentAmount is not None else current.get("reserve_current_amount", 0)
    )

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE settings
            SET monthly_income = %s,
                daily_goal = %s,
                reserve_amount = %s,
                reserve_goal_amount = %s,
                reserve_current_amount = %s
            WHERE user_id = %s AND id = 1
            RETURNING *
            """,
            (monthly_income, daily_goal, reserve_amount, reserve_goal_amount, reserve_current_amount, user_id),
        )
        row = normalize_row(cursor.fetchone())
    return row or get_settings(user_id)


@app.post("/api/categories")
def create_category(payload: CategoryPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    name = clean_text(payload.name, "Nome da categoria", 80)
    color = validate_hex_color(payload.color)
    icon = clean_text(payload.icon, "\u00cdcone", 10)

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                "SELECT id, is_active FROM categories WHERE user_id = %s AND name = %s",
                (user_id, name),
            )
            existing = normalize_row(cursor.fetchone())

            if existing and existing["is_active"]:
                # SEC-03: o antigo ON CONFLICT ... DO UPDATE SET type = EXCLUDED.type
                # reescrevia o type de uma categoria ATIVA existente, reclassificando
                # em massa todo o hist\u00f3rico ligado a ela. Nome j\u00e1 em uso por uma
                # categoria ativa \u00e9 conflito, n\u00e3o atualiza\u00e7\u00e3o.
                raise HTTPException(status_code=409, detail="Categoria j\u00e1 existe.")

            if existing:
                # Reativa a categoria arquivada. O type NUNCA muda aqui pelo
                # mesmo motivo acima \u2014 s\u00f3 is_active, color e icon acompanham a
                # escolha atual do usu\u00e1rio.
                cursor.execute(
                    """
                    UPDATE categories
                    SET color = %s, icon = %s, is_active = TRUE, updated_at = NOW()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (color, icon, existing["id"]),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO categories (user_id, name, type, color, icon, is_default, is_active)
                    VALUES (%s, %s, %s, %s, %s, 0, TRUE)
                    RETURNING *
                    """,
                    (user_id, name, payload.type, color, icon),
                )
            row = require_row(normalize_row(cursor.fetchone()), "Categoria n\u00e3o criada.")
    except errors.UniqueViolation:
        raise HTTPException(status_code=409, detail="Categoria j\u00e1 existe.") from None

    return row


@app.delete("/api/categories/{category_id}")
def delete_category(category_id: int, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            SELECT id, is_default
            FROM categories
            WHERE user_id = %s AND id = %s AND COALESCE(is_active, TRUE) = TRUE
            """,
            (user_id, category_id),
        )
        category = normalize_row(cursor.fetchone())
        if not category:
            raise HTTPException(status_code=404, detail="Categoria n\u00e3o encontrada.")

        cursor.execute(
            "SELECT COUNT(*) AS total FROM transactions WHERE user_id = %s AND category_id = %s",
            (user_id, category_id),
        )
        linked_transactions = int(require_row(normalize_row(cursor.fetchone()), "V\u00ednculos n\u00e3o encontrados.")["total"])

        cursor.execute("DELETE FROM budgets WHERE user_id = %s AND category_id = %s", (user_id, category_id))
        cursor.execute("DELETE FROM categorization_rules WHERE user_id = %s AND category_id = %s", (user_id, category_id))

        should_archive = linked_transactions > 0 or int(category.get("is_default") or 0) == 1
        if should_archive:
            cursor.execute(
                "UPDATE categories SET is_active = FALSE WHERE user_id = %s AND id = %s",
                (user_id, category_id),
            )
            return {"deleted": False, "archived": True, "linkedTransactions": linked_transactions}

        cursor.execute("DELETE FROM categories WHERE user_id = %s AND id = %s", (user_id, category_id))
        return {"deleted": True, "archived": False, "linkedTransactions": 0}


@app.post("/api/transactions")
def create_transaction(payload: TransactionPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    title = clean_text(payload.title, "T\u00edtulo", 200)
    payment_method = clean_text(payload.paymentMethod, "Forma de pagamento", 50)
    notes = clean_text(payload.notes, "Observa\u00e7\u00f5es", 1000, required=False)
    transaction_date = validate_date_text(payload.transactionDate, "Data")
    billing_month = validate_month_text(payload.billingMonth)
    if payload.cardId and not billing_month:
        # DOM-03: sem isto, uma compra avulsa no cartão feita depois do
        # fechamento caía na fatura do mês da compra em vez da seguinte —
        # o mesmo cálculo que create_installments já faz para parceladas.
        # Card inexistente é deixado para a FK violation abaixo (mesmo
        # comportamento de erro que já existia); aqui só ajusta o mês
        # quando o cartão é encontrado.
        with db_cursor() as cursor:
            cursor.execute(
                "SELECT closing_day FROM cards WHERE user_id = %s AND id = %s",
                (user_id, payload.cardId),
            )
            card_row = normalize_row(cursor.fetchone())
        if card_row:
            billing_month = first_billing_month(transaction_date, card_row.get("closing_day"))
    is_recurring, recurrence_type, recurrence_day = normalize_recurrence(
        payload.isRecurring,
        payload.recurrenceType,
        payload.recurrenceDay,
        transaction_date,
    )

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                INSERT INTO transactions
                  (user_id, title, amount, type, category_id, payment_method, transaction_date, notes, card_id, billing_month,
                   installment_group, installment_number, total_installments, is_recurring, recurrence_type, recurrence_day, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, %s, %s, %s, 'manual')
                RETURNING *
                """,
                (
                    user_id,
                    title,
                    round_money(payload.amount),
                    payload.type,
                    payload.categoryId,
                    payment_method,
                    transaction_date,
                    notes,
                    payload.cardId,
                    billing_month,
                    is_recurring,
                    recurrence_type,
                    recurrence_day,
                ),
            )
            row = require_row(normalize_row(cursor.fetchone()), "Lan\u00e7amento n\u00e3o criado.")
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria ou cart\u00e3o inv\u00e1lido.") from None

    return row


@app.put("/api/transactions/{transaction_id}")
def update_transaction(
    transaction_id: int,
    payload: TransactionUpdatePayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT *
            FROM transactions
            WHERE user_id = %s AND id = %s
            """,
            (user_id, transaction_id),
        )
        current = normalize_row(cursor.fetchone())
    if not current:
        raise HTTPException(status_code=404, detail="Lançamento não encontrado.")

    title = clean_text(payload.title, "Titulo", 200) if "title" in fields_set and payload.title else current["title"]
    amount = round_money(payload.amount if "amount" in fields_set and payload.amount is not None else current["amount"])
    transaction_type = payload.type if "type" in fields_set and payload.type else current["type"]
    category_id = payload.categoryId if "categoryId" in fields_set else current["category_id"]
    payment_method = (
        clean_text(payload.paymentMethod, "Forma de pagamento", 50)
        if "paymentMethod" in fields_set and payload.paymentMethod
        else current["payment_method"]
    )
    transaction_date = (
        validate_date_text(payload.transactionDate, "Data")
        if "transactionDate" in fields_set and payload.transactionDate
        else current["transaction_date"]
    )
    notes = (
        clean_text(payload.notes or "", "Observações", 1000, required=False)
        if "notes" in fields_set
        else current.get("notes", "")
    )
    card_id = payload.cardId if "cardId" in fields_set else current["card_id"]
    billing_month = validate_month_text(payload.billingMonth) if "billingMonth" in fields_set else current["billing_month"]

    if current["installment_group"]:
        # FIN-09: mudar type, valor ou mês de fatura de UMA parcela quebra a
        # integridade do grupo inteiro — a soma deixa de bater com o total
        # parcelado, e o mês de cobrança sai da sequência esperada. Título,
        # categoria, forma de pagamento e notas continuam livres.
        if transaction_type != current["type"]:
            raise HTTPException(status_code=409, detail="Não é possível alterar o tipo de uma parcela isoladamente.")
        if amount != round_money(current["amount"]):
            raise HTTPException(status_code=409, detail="Não é possível alterar o valor de uma parcela isoladamente.")
        if billing_month != current["billing_month"]:
            raise HTTPException(
                status_code=409, detail="Não é possível alterar o mês de fatura de uma parcela isoladamente."
            )

    # FIN-09: um lançamento importado editado (título, valor, data ou tipo)
    # deixava o duplicate_hash obsoleto — a próxima importação do mesmo
    # extrato reintroduziria a transação, já que o hash guardado não batia
    # mais com o conteúdo atual da linha.
    duplicate_hash = current.get("duplicate_hash")
    if current.get("source") == "csv_import":
        duplicate_hash = build_duplicate_hash(user_id, transaction_date, title, amount, transaction_type)

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                UPDATE transactions
                SET title = %s,
                    amount = %s,
                    type = %s,
                    category_id = %s,
                    payment_method = %s,
                    transaction_date = %s,
                    notes = %s,
                    card_id = %s,
                    billing_month = %s,
                    duplicate_hash = %s
                WHERE user_id = %s AND id = %s
                RETURNING *
                """,
                (
                    title,
                    amount,
                    transaction_type,
                    category_id,
                    payment_method,
                    transaction_date,
                    notes,
                    card_id,
                    billing_month,
                    duplicate_hash,
                    user_id,
                    transaction_id,
                ),
            )
            row = require_row(normalize_row(cursor.fetchone()), "Lançamento não atualizado.")
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria ou cartão inválido.") from None
    except errors.UniqueViolation:
        # O duplicate_hash recalculado (source = csv_import) pode colidir com
        # outra transação já existente se a edição a tornar idêntica a ela.
        raise HTTPException(
            status_code=409, detail="Já existe um lançamento idêntico (mesma data, descrição e valor)."
        ) from None

    return row


@app.post("/api/transactions/{transaction_id}/set-recurring")
def set_transaction_recurring(
    transaction_id: int,
    payload: RecurringPayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    is_recurring, recurrence_type, recurrence_day = normalize_recurrence(
        payload.is_recurring,
        payload.recurrence_type,
        payload.recurrence_day,
    )

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE transactions
            SET is_recurring = %s, recurrence_type = %s, recurrence_day = %s
            WHERE user_id = %s AND id = %s
            RETURNING *
            """,
            (is_recurring, recurrence_type, recurrence_day, user_id, transaction_id),
        )
        row = normalize_row(cursor.fetchone())

    if not row:
        raise HTTPException(status_code=404, detail="Lan\u00e7amento n\u00e3o encontrado.")
    return row


def get_export_transactions(user_id: str, month_key: str) -> list[dict]:
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT t.transaction_date, t.title, c.name AS category_name, t.type, t.amount,
                   t.payment_method, t.source, t.notes, cards.name AS card_name,
                   t.installment_number, t.total_installments
            FROM transactions t
            LEFT JOIN categories c ON c.id = t.category_id AND c.user_id = t.user_id
            LEFT JOIN cards ON cards.id = t.card_id AND cards.user_id = t.user_id
            WHERE t.user_id = %s
              AND COALESCE(t.billing_month, substring(t.transaction_date from 1 for 7)) = %s
            ORDER BY t.transaction_date ASC, t.id ASC
            """,
            (user_id, month_key),
        )
        return normalize_rows(cursor.fetchall())


@app.get("/api/export/csv")
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


@app.get("/api/export/pdf")
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


# Deprecated compatibility endpoints. The current frontend uses Parcelas and does not expose card registration.
@app.post("/api/cards")
def create_card(payload: CardPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    name = clean_text(payload.name, "Nome do cart\u00e3o", 100)
    brand = clean_text(payload.brand, "Bandeira", 40)
    last_four = clean_text(payload.lastFour, "Final do cart\u00e3o", 4)
    if not last_four.isdigit():
        raise HTTPException(status_code=400, detail="Final do cart\u00e3o deve conter 4 n\u00fameros.")
    color = validate_hex_color(payload.color)

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            INSERT INTO cards (user_id, name, brand, last_four, credit_limit, closing_day, due_day, color)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (user_id, name, brand, last_four, round_money(payload.creditLimit), payload.closingDay, payload.dueDay, color),
        )
        row = require_row(normalize_row(cursor.fetchone()), "Cart\u00e3o n\u00e3o criado.")
    return row


@app.put("/api/cards/{card_id}")
def update_card(card_id: int, payload: CardUpdatePayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    current = get_card_for_user(user_id, card_id)
    fields_set = getattr(payload, "model_fields_set", getattr(payload, "__fields_set__", set()))
    name = clean_text(payload.name, "Nome do cartão", 100) if "name" in fields_set and payload.name else current["name"]
    brand = clean_text(payload.brand, "Bandeira", 40) if "brand" in fields_set and payload.brand else current["brand"]
    last_four = clean_text(payload.lastFour, "Final do cartão", 4) if "lastFour" in fields_set and payload.lastFour else current["last_four"]
    if not str(last_four).isdigit():
        raise HTTPException(status_code=400, detail="Final do cartão deve conter 4 números.")
    credit_limit = round_money(
        payload.creditLimit if "creditLimit" in fields_set and payload.creditLimit is not None else current["credit_limit"]
    )
    closing_day = payload.closingDay if "closingDay" in fields_set and payload.closingDay else current["closing_day"]
    due_day = payload.dueDay if "dueDay" in fields_set and payload.dueDay else current["due_day"]
    color = validate_hex_color(payload.color) if "color" in fields_set and payload.color else current["color"]

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            UPDATE cards
            SET name = %s,
                brand = %s,
                last_four = %s,
                credit_limit = %s,
                closing_day = %s,
                due_day = %s,
                color = %s
            WHERE user_id = %s AND id = %s
            RETURNING *
            """,
            (name, brand, last_four, credit_limit, closing_day, due_day, color, user_id, card_id),
        )
        return require_row(normalize_row(cursor.fetchone()), "Cartão não atualizado.")


@app.delete("/api/cards/{card_id}")
def delete_card(
    card_id: int,
    force: bool = False,
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]
    get_card_for_user(user_id, card_id)
    with db_cursor(commit=True) as cursor:
        cursor.execute(
            "SELECT COUNT(*) AS total FROM transactions WHERE user_id = %s AND card_id = %s",
            (user_id, card_id),
        )
        linked = int(require_row(normalize_row(cursor.fetchone()), "Cartão não encontrado.")["total"])
        if linked and not force:
            raise HTTPException(
                status_code=409,
                detail="Cartão possui lançamentos vinculados. Revise antes de excluir ou use force=true.",
            )
        if linked and force:
            cursor.execute(
                "UPDATE transactions SET card_id = NULL WHERE user_id = %s AND card_id = %s",
                (user_id, card_id),
            )
        cursor.execute("DELETE FROM card_pins WHERE user_id = %s AND card_id = %s", (user_id, card_id))
        cursor.execute("DELETE FROM cards WHERE user_id = %s AND id = %s", (user_id, card_id))
    invalidate_card_unlock_sessions(user_id, card_id)
    clear_card_pin_failures(user_id, card_id)
    audit_log("card_deleted", user_id, {"card_id": card_id, "linked_transactions": linked, "force": force})
    return {"deleted": True, "unlinkedTransactions": linked if force else 0}


@app.post("/api/cards/{card_id}/installments")
def create_installments(card_id: int, payload: InstallmentPayload, current_user: dict = Depends(get_current_user)) -> dict:
    user_id = current_user["id"]
    title = clean_text(payload.title, "Descri\u00e7\u00e3o da compra", 200)
    notes = clean_text(payload.notes, "Observa\u00e7\u00f5es", 1000, required=False)
    purchase_date = validate_date_text(payload.purchaseDate, "Data da compra")
    try:
        installment_amounts = distribute_installments(payload.totalAmount, payload.totalInstallments)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        with db_cursor(commit=True) as cursor:
            cursor.execute(
                """
                SELECT *
                FROM cards
                WHERE user_id = %s AND id = %s
                """,
                (user_id, card_id),
            )
            card = normalize_row(cursor.fetchone())
            if not card:
                raise HTTPException(status_code=404, detail="Cart\u00e3o n\u00e3o encontrado.")

            # Compra feita depois do fechamento entra na fatura do m\u00eas seguinte.
            first_month = first_billing_month(purchase_date, card.get("closing_day"))
            # UUID, n\u00e3o uma chave derivada de (user, cart\u00e3o, t\u00edtulo, data): duas
            # compras iguais no mesmo dia (duas passagens, dois notebooks da
            # fam\u00edlia) colidiam no mesmo grupo, duplicando installment_number e
            # fazendo a exclus\u00e3o de uma apagar as duas (DOM-01).
            group = str(uuid.uuid4())
            for number, amount in enumerate(installment_amounts, start=1):
                cursor.execute(
                    """
                    INSERT INTO transactions
                      (user_id, title, amount, type, category_id, payment_method, transaction_date, notes, card_id,
                       billing_month, installment_group, installment_number, total_installments, source)
                    VALUES (%s, %s, %s, 'expense', %s, 'cr\u00e9dito', %s, %s, %s, %s, %s, %s, %s, 'manual')
                    """,
                    (
                        user_id,
                        title,
                        amount,
                        payload.categoryId,
                        purchase_date,
                        notes,
                        card_id,
                        add_months(first_month, number - 1),
                        group,
                        number,
                        payload.totalInstallments,
                    ),
                )

            cursor.execute(
                """
                SELECT *
                FROM transactions
                WHERE user_id = %s AND installment_group = %s
                ORDER BY installment_number ASC
                """,
                (user_id, group),
            )
            rows = normalize_rows(cursor.fetchall())
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria ou cart\u00e3o inv\u00e1lido.") from None

    return {
        "createdInstallments": len(rows),
        "group": group,
        "rows": rows,
    }


@app.post("/api/installments/simulate")
def simulate_installments(
    payload: InstallmentSimulationPayload,
    _current_user: dict = Depends(get_current_user),
) -> dict:
    """Simula o impacto de uma compra parcelada sem exigir cartão cadastrado."""
    purchase_date = validate_date_text(payload.purchaseDate, "Data da compra")
    base_month = month_key_from_date(purchase_date)
    
    # DOM-02: apply_installment_interest nunca arredonda a taxa para
    # centavos antes de aplicá-la (round_money(Decimal(rate)/100) fazia
    # 0,4% a.m. virar 0,00% e os juros sumirem).
    try:
        installment_amounts = apply_installment_interest(
            payload.totalAmount, payload.totalInstallments, payload.interestRate
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    
    simulated_by_month = {
        add_months(base_month, index): amount for index, amount in enumerate(installment_amounts)
    }
    
    projection = []
    for month_offset in range(payload.months):
        month_key = add_months(base_month, month_offset)
        simulated_amount = round_money(simulated_by_month.get(month_key, Decimal("0")))
        projection.append({
            "month": month_key,
            "simulatedInstallment": simulated_amount,
            "projectedTotal": simulated_amount,
        })
    
    return {
        "totalAmount": round_money(payload.totalAmount),
        "totalInstallments": payload.totalInstallments,
        "interestRate": payload.interestRate,
        "installments": [float(x) for x in installment_amounts],
        "projection": projection,
    }


@app.post("/api/installments")
def create_installments_without_card(
    payload: InstallmentWithoutCardPayload,
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Cria uma compra parcelada sem exigir cartão cadastrado."""
    user_id = current_user["id"]
    title = clean_text(payload.title, "Descrição da compra", 200)
    notes = clean_text(payload.notes, "Observações", 1000, required=False)
    purchase_date = validate_date_text(payload.purchaseDate, "Data da compra")
    base_month = month_key_from_date(purchase_date)
    
    # DOM-02: apply_installment_interest nunca arredonda a taxa para
    # centavos antes de aplicá-la (round_money(Decimal(rate)/100) fazia
    # 0,4% a.m. virar 0,00% e os juros sumirem).
    try:
        installment_amounts = apply_installment_interest(
            payload.totalAmount, payload.totalInstallments, payload.interestRate
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    
    try:
        with db_cursor(commit=True) as cursor:
            # UUID pelo mesmo motivo de create_installments (DOM-01).
            group = str(uuid.uuid4())
            for number, amount in enumerate(installment_amounts, start=1):
                cursor.execute(
                    """
                    INSERT INTO transactions
                      (user_id, title, amount, type, category_id, payment_method, transaction_date, notes,
                       billing_month, installment_group, installment_number, total_installments, source)
                    VALUES (%s, %s, %s, 'expense', %s, 'compra parcelada', %s, %s, %s, %s, %s, %s, 'manual')
                    """,
                    (
                        user_id,
                        title,
                        amount,
                        payload.categoryId,
                        purchase_date,
                        notes,
                        add_months(base_month, number - 1),
                        group,
                        number,
                        payload.totalInstallments,
                    ),
                )
    except errors.ForeignKeyViolation:
        raise HTTPException(status_code=400, detail="Categoria inválida.") from None
    
    return {
        "createdInstallments": payload.totalInstallments,
        "group": group,
        "totalAmount": round_money(payload.totalAmount),
    }


@app.get("/api/installments/future")
def get_future_installments(
    month: str = Query(..., min_length=7, max_length=7),
    limit: int = Query(default=24, ge=1, le=48),
    current_user: dict = Depends(get_current_user),
) -> dict:
    """Retorna parcelas futuras do usuário."""
    user_id = current_user["id"]
    
    with db_cursor() as cursor:
        cursor.execute(
            """
            SELECT 
                t.id,
                t.installment_group,
                billing_month,
                title,
                COALESCE(c.name, 'Sem categoria') as category_name,
                amount,
                installment_number,
                total_installments
            FROM transactions t
            LEFT JOIN categories c ON t.category_id = c.id
            WHERE t.user_id = %s 
              AND t.type = 'expense'
              AND t.installment_group IS NOT NULL
              AND billing_month >= %s
            ORDER BY billing_month ASC, installment_number ASC
            LIMIT %s
            """,
            (user_id, month, limit),
        )
        rows = normalize_rows(cursor.fetchall())
    
    installments = []
    total_by_month = {}
    
    for row in rows:
        installments.append({
            "id": row["id"],
            "group": row["installment_group"],
            "month": row["billing_month"],
            "title": row["title"],
            "categoryName": row["category_name"],
            "amount": float(row["amount"]),
            "installmentNumber": row["installment_number"],
            "totalInstallments": row["total_installments"],
        })
        
        month_key = row["billing_month"]
        if month_key not in total_by_month:
            total_by_month[month_key] = Decimal("0")
        total_by_month[month_key] += Decimal(str(row["amount"]))
    
    # Calcular média mensal
    total_commitment = sum(total_by_month.values()) if total_by_month else Decimal("0")
    avg_monthly = round_money(total_commitment / len(total_by_month)) if total_by_month else Decimal("0")
    
    return {
        "installments": installments,
        "totalMonthlyCommitment": float(avg_monthly),
    }


@app.delete("/api/transactions/{transaction_id}")
def delete_transaction(
    transaction_id: int,
    scope: Literal["single", "group"] = "single",
    current_user: dict = Depends(get_current_user),
) -> dict:
    user_id = current_user["id"]

    with db_cursor(commit=True) as cursor:
        cursor.execute(
            """
            SELECT *
            FROM transactions
            WHERE user_id = %s AND id = %s
            """,
            (user_id, transaction_id),
        )
        tx = normalize_row(cursor.fetchone())
        if not tx:
            raise HTTPException(status_code=404, detail="Lan\u00e7amento n\u00e3o encontrado.")

        if tx["installment_group"]:
            if scope != "group":
                # FIN-10: apagar uma parcela apagava o grupo inteiro sem
                # sinalizar isso antes \u2014 a API s\u00f3 avisava depois, com
                # deletedGroup: true. Agora \u00e9 preciso confirmar explicitamente.
                cursor.execute(
                    """
                    SELECT COUNT(*) AS total
                    FROM transactions
                    WHERE user_id = %s AND installment_group = %s
                    """,
                    (user_id, tx["installment_group"]),
                )
                total = int(
                    require_row(normalize_row(cursor.fetchone()), "Contagem de parcelas n\u00e3o encontrada.")["total"]
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"Este lan\u00e7amento faz parte de um parcelamento com {total} parcela(s). "
                        "Envie scope=group para excluir todas."
                    ),
                )
            cursor.execute(
                """
            DELETE FROM transactions
            WHERE user_id = %s AND installment_group = %s
            """,
                (user_id, tx["installment_group"]),
            )
            audit_log("transaction_deleted", user_id, {"transaction_id": transaction_id, "group": True})
            return {"deletedGroup": True}

        cursor.execute(
            """
            DELETE FROM transactions
            WHERE user_id = %s AND id = %s
            """,
            (user_id, transaction_id),
        )
    audit_log("transaction_deleted", user_id, {"transaction_id": transaction_id, "group": False})
    return {"deleted": True}


if FRONTEND_OUT_DIR.exists():
    next_static_dir = FRONTEND_OUT_DIR / "_next"
    if next_static_dir.exists():
        app.mount("/_next", StaticFiles(directory=next_static_dir), name="next-static")

    @app.get("/")
    def frontend_index() -> FileResponse:
        return FileResponse(FRONTEND_OUT_DIR / "index.html")

    @app.get("/{frontend_path:path}")
    def frontend_route(frontend_path: str) -> FileResponse:
        if frontend_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Rota não encontrada.")

        path = Path(frontend_path)
        if path.is_absolute() or ".." in path.parts:
            raise HTTPException(status_code=404, detail="Arquivo não encontrado.")

        direct_file = FRONTEND_OUT_DIR / path
        if direct_file.is_file():
            return FileResponse(direct_file)

        html_file = FRONTEND_OUT_DIR / f"{frontend_path.rstrip('/')}.html"
        if html_file.is_file():
            return FileResponse(html_file)

        nested_index = FRONTEND_OUT_DIR / path / "index.html"
        if nested_index.is_file():
            return FileResponse(nested_index)

        return FileResponse(FRONTEND_OUT_DIR / "404.html", status_code=404)
else:

    @app.get("/")
    def api_root() -> dict:
        return {"service": "Trevo API", "docs": "/docs", "health": "/api/health"}
