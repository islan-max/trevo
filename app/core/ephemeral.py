"""Estado efêmero em memória de processo.

Cada dict/set aqui é um FALLBACK usado quando o Postgres não está disponível
(ou, para alguns, um cache local sempre consultado antes do banco). Em
serverless, cada instância tem o seu próprio — não são compartilhados entre
processos, então nunca substituem a persistência em Postgres, só evitam que
uma falha transitória do banco derrube rate limit, PIN de cartão, sessão de
desbloqueio, importação de CSV ou revogação de token por completo.
"""
from __future__ import annotations

from typing import Any

card_pin_failures: dict[str, dict[str, Any]] = {}
card_unlock_sessions: dict[str, dict[str, Any]] = {}
csv_import_sessions: dict[str, dict[str, Any]] = {}
login_failures: dict[str, dict[str, Any]] = {}
revoked_token_hashes: set[str] = set()

# Fallback em memória de processo para enforce_ip_rate_limit, usado só quando
# o banco está fora do ar (fail-open não significa "sem limite nenhum").
ip_rate_limit_fallback: dict[str, dict[str, Any]] = {}
