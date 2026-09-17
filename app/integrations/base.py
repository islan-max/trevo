"""Contrato de desenho para uma futura integração Open Finance/agregador
bancário — não é código vivo hoje (BP-12 trata da decisão de fornecedor;
nada aqui é importado em runtime). `FinancialDataSource` e
`ImportedTransaction` documentam a forma que uma integração real deveria
assumir: buscar transações já normalizadas, sem persistir nada, para o
chamador então rodar deduplicação e categorização com o resto do pipeline
de importação (ver app/integrations/normalizer.py, usado hoje pela
importação de CSV, e app/imports/service.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class ImportedTransaction:
    external_id: str | None
    transaction_date: str
    description: str
    amount: Decimal
    source: str
    raw_payload: dict


class FinancialDataSource(Protocol):
    source_name: str

    def fetch_transactions(self, user_id: str, month: str) -> list[ImportedTransaction]:
        """Return normalized transactions for a user/month without persisting them."""
