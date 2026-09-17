"""Health checks. Ficam fora de qualquer domínio de negócio — são
infraestrutura da aplicação, não uma funcionalidade do produto.
"""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.api.deps import PlainDictRoute
from app.core.config import APP_VERSION
from app.core.database import db_cursor
from app.core.signing import secret_source

logger = logging.getLogger("trevo")

router = APIRouter(route_class=PlainDictRoute)

# Atualizado por main.py::lifespan a cada start do processo.
startup_time = time.time()


@router.get("/api/health/live")
def liveness():
    # Liveness: process is up. No dependencies checked (no DB), so it stays green
    # during transient database blips — used to decide restarts, not readiness.
    return {"ok": True, "status": "alive", "uptime_seconds": int(time.time() - startup_time)}


@router.get("/api/health")
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
            "version": APP_VERSION,
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
                "version": APP_VERSION,
                "uptime_seconds": int(time.time() - startup_time),
                "checks": {
                    "database": {"status": "error", "latency_ms": None},
                    "migrations": {"status": "unknown"},
                    "signing": {"source": secret_source()},
                },
            },
            status_code=503,
        )
