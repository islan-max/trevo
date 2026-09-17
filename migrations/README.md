# Migrations

## Convenção de numeração

- Arquivos são `NNNN_descricao_curta.sql`, aplicados em ordem alfabética
  (numérica) por `migrate.py::apply_versioned_migrations`.
- `0000_baseline.sql` é especial: contém o schema completo do projeto (todas
  as tabelas, índices, triggers e constraints da linha de base), extraído de
  `SCHEMA_SQL` em `migrate.py` no BP-08 da auditoria técnica
  (`docs/auditoria-2026-09.md`). Antes disso, esse SQL rodava fora do
  controle de versão — sem registro em `schema_migrations`, incondicionalmente
  a cada `migrate.py` (e, em serverless, a cada cold start via
  `ensure_serverless_schema`). Ver OPS-01 na auditoria.
- Toda migration deve ser idempotente (`IF NOT EXISTS` / `IF EXISTS`) — o
  histórico de deploys inclui bancos que já têm partes do schema aplicadas
  por fora do fluxo normal.
- Uma migration por transação: `apply_versioned_migrations` comita depois de
  cada arquivo, então uma falha no meio do lote deixa as anteriores
  registradas e a próxima tentativa recomeça do ponto certo.

## Bancos já provisionados

`migrate.py::backfill_baseline_if_provisioned` detecta um banco que já tem o
schema (verifica a existência da tabela `users`) e registra
`0000_baseline` em `schema_migrations` **sem** executar a DDL dela — ela é
idempotente, mas inclui um `DROP CONSTRAINT` + `ADD CONSTRAINT` em
`transactions` que toma `ACCESS EXCLUSIVE` e revalida a tabela inteira. Rodar
isso de novo em produção seria apenas um custo desnecessário, já pago
inúmeras vezes no regime anterior.

Num banco vazio, a mesma checagem detecta que `users` não existe e deixa
`0000_baseline.sql` aplicar normalmente, criando o schema do zero.

## Sobre a ausência de `0007`

A sequência de migrations salta de `0006_category_archival.sql` para
`0008_lgpd.sql` — não existe (e nunca existiu) um `0007_*.sql` neste
repositório. `git log --diff-filter=D -- migrations/` não mostra nenhuma
remoção de arquivo em `migrations/`, então o número não corresponde a uma
migration que foi criada e depois apagada: ele nunca chegou a virar commit
neste repositório (o cenário mais provável é uma numeração reservada durante
o desenvolvimento — por exemplo um branch ou rebase que não chegou a ser
mesclado — e nunca reaproveitada).

Isto **não foi confirmado contra o `schema_migrations` de produção** (fora do
alcance deste ambiente de trabalho); antes de reaproveitar `0007` para uma
migration nova, rode:

```sql
SELECT version FROM schema_migrations WHERE version LIKE '0007%';
```

Se a consulta não retornar nada, o número está livre. Se retornar algo,
**pare e relate** — significa que uma migration `0007` existiu em produção
sem nunca ter sido commitada neste repositório, o que é uma divergência mais
séria do que um número pulado.
