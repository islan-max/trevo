"""Fonte única de "agora" para decisões de negócio (DOM-04).

`datetime.now(UTC)` está certo para timestamps de persistência — created_at,
auditoria, expiração de token, janelas de rate limit — mas está ERRADO para
decidir "que dia é hoje" ou "qual é o mês atual" num produto pt-BR: das 21h à
meia-noite em America/Sao_Paulo (UTC-3), já é o dia seguinte em UTC. O
servidor trocava de mês contábil e de "dia do progresso" três horas antes da
virada real no Brasil.

Use `today()`/`now()`/`current_month()` daqui para qualquer cálculo que
dependa de "agora" do ponto de vista do usuário (mês corrente, dia do
calendário de metas). Continue usando `datetime.now(UTC)` diretamente para
timestamps que são apenas registrados ou usados em aritmética de duração
(auditoria, expiração de sessão, janelas de rate limit) — fuso não muda o
resultado desses casos, e misturar os dois ali só adicionaria uma conversão
sem propósito.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

_DEFAULT_TIMEZONE = "America/Sao_Paulo"


def _timezone() -> ZoneInfo:
    # Lido a cada chamada (não cacheado no import) para permitir configurar
    # ou trocar em teste, no mesmo espírito de app/core/config.py.
    return ZoneInfo(os.getenv("APP_TIMEZONE", _DEFAULT_TIMEZONE))


def now() -> datetime:
    """Agora, no fuso de negócio configurado (aware)."""
    return datetime.now(UTC).astimezone(_timezone())


def today() -> date:
    """Data de hoje, do ponto de vista do fuso de negócio configurado."""
    return now().date()


def current_month() -> str:
    """Mês corrente no formato AAAA-MM, no fuso de negócio configurado."""
    return today().strftime("%Y-%m")
