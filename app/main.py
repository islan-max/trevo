from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi.errors import RateLimitExceeded
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import health as health_module
from app.api.deps import PlainDictRoute, limiter, rate_limit_handler
from app.api.health import router as health_router
from app.api.middleware import add_request_id, add_security_headers, csrf_protect, validate_content_type
from app.auth.router import router as auth_router
from app.budgets.router import router as budgets_router
from app.cards.router import router as cards_router
from app.categories.router import router as categories_router
from app.core import storage
from app.core.config import APP_VERSION, settings
from app.core.database import close_db_pool, connection, get_database_url, init_db_pool
from app.core.logging import JsonLogFormatter
from app.core.startup import validate_runtime_config
from app.dashboard.router import router as dashboard_router
from app.goals.router import router as goals_router
from app.imports.router import router as imports_router
from app.privacy.router import router as privacy_router
from app.reports.router import router as reports_router
from app.transactions.router import router as transactions_router
from app.users.router import router as users_router
from migrate import run_migrations

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_OUT_DIR = BASE_DIR / "frontend" / "out"

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

ALLOWED_ORIGINS = settings.allowed_origins
if not settings.is_serverless:
    storage.PROFILE_PHOTO_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ciclo de vida do processo.

    Substitui os antigos `@app.on_event`, removidos nas versões novas do
    Starlette — era por isso que a suíte quebrava com
    `'APIRouter' object has no attribute 'startup'`.
    """
    health_module.startup_time = time.time()
    logger.info("Starting Trevo")
    # Serverless (Vercel) valida config e conecta por request; validar, abrir
    # pool e migrar no cold start derrubaria a função inteira se faltasse env.
    # Lá o schema é só verificado, via ensure_serverless_schema no primeiro
    # request (app/core/database.py) — migrations rodam no build (BP-08).
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


app = FastAPI(title="Trevo API", version=APP_VERSION, lifespan=lifespan)
app.router.route_class = PlainDictRoute
app.state.limiter = limiter
if not settings.is_serverless:
    app.mount(storage.PROFILE_PHOTO_URL_PREFIX, StaticFiles(directory=storage.PROFILE_PHOTO_DIR), name="profile-photos")

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

# Middlewares HTTP na mesma ordem de antes da extração em routers (BP-09):
# add_request_id, validate_content_type, csrf_protect, add_security_headers.
app.middleware("http")(add_request_id)
app.middleware("http")(validate_content_type)
app.middleware("http")(csrf_protect)
app.middleware("http")(add_security_headers)

app.include_router(health_router)
app.include_router(auth_router)
app.include_router(privacy_router)
app.include_router(categories_router)
app.include_router(cards_router)
app.include_router(transactions_router)
app.include_router(budgets_router)
app.include_router(goals_router)
app.include_router(users_router)
app.include_router(imports_router)
app.include_router(reports_router)
app.include_router(dashboard_router)


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
