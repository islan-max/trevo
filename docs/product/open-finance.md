# Open Finance Future

Nao ha integracao bancaria real neste projeto. Decisao registrada: ver
[docs/architecture/adr-open-finance.md](../architecture/adr-open-finance.md)
(BP-12 da auditoria tecnica) — ADIADO, com data de reavaliacao e o motivo
(custo fixo do agregador desproporcional ao estagio atual do produto).

Preparacao atual:

- `app/integrations/base.py` — contrato de desenho (`FinancialDataSource`,
  `ImportedTransaction`), nao importado por ninguem hoje.
- `app/integrations/normalizer.py` — pipeline real de normalizacao/hash de
  duplicata, ja usado pela importacao de CSV.
- `app/integrations/open_finance/README.md`
- Valor de source `open_finance_future` (aceito na constraint, nunca
  gravado hoje).

Fluxo futuro recomendado (ver o ADR para a modelagem completa de
`connections`/`sync_runs`):

1. Conectar agregador licenciado (Pluggy ou Belvo — ver ADR).
2. Buscar transacoes por usuario autorizado.
3. Normalizar para `ImportedTransaction`.
4. Rodar pelo mesmo pipeline de deduplicacao que o CSV usa
   (`app/integrations/normalizer.py`, `app/imports/service.py`).
5. Salvar com `source = open_finance_future`.
6. Permitir revisao do usuario antes de misturar com dados manuais.
