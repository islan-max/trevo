# Changelog

Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/).

## [Unreleased]

## [2.0.0] - 2026-09-17

Primeira versão tagueada — reúne a auditoria técnica completa
(`docs/auditoria-2026-09.md`) e os breakpoints de correção que vieram dela
(BP-05 a BP-09), além do trabalho de rebranding e LGPD documentado abaixo.

### Added (auditoria técnica — BP-05 a BP-09)

- **Autenticação/OAuth (BP-05):** vinculação de conta social exige senha
  confirmada (`?link=true`, autenticado); segredo de assinatura provisionado
  automaticamente quando `JWT_SECRET_KEY` falta; sessão de cookie renova de
  forma deslizante perto de expirar; revogação de conta desativada por LGPD.
- **CSV (BP-04):** parsing tolerante a preâmbulo de banco, cp1252, delimitador
  por tabulação/pipe; hash de duplicata em 3 níveis (título+valor+data,
  +tipo, +hora/conta).
- **Domínio financeiro (BP-03):** fuso horário `America/Sao_Paulo` em vez de
  UTC para "hoje"/mês corrente; renda efetiva (não soma mais renda configurada
  com lançamentos de entrada); parcelamento com fechamento de cartão correto.
- **Paginação (BP-08):** `GET /api/transactions` aceita `limit`/`offset` e
  devolve `hasMore`, em vez de truncar em 250 linhas sem avisar.
- **Manifesto PWA e resiliência de frontend (BP-07):** `app/error.tsx`,
  `global-error.tsx`, `not-found.tsx`; diálogo acessível compartilhado
  (`components/Dialog.tsx`); `/relatorios` carrega gráficos sob demanda.

### Changed (auditoria técnica — BP-06 a BP-09)

- **Performance (BP-06):** `/api/bootstrap` deixou de escalar com o número de
  cartões/parcelas (batches de 2 a 4 queries em vez de dezenas); uma única
  conexão por request em serverless, não uma por query.
- **Schema versionado (BP-08):** todo o DDL histórico (antes solto em
  `migrate.py`) virou `migrations/0000_baseline.sql`, aplicada como qualquer
  outra migration — nunca mais reexecutada a cada cold start serverless.
  `card_pins` ganhou a FK composta `(user_id, card_id)` que todo o resto do
  projeto já usava; `categories.role` substitui a checagem de score por nome
  de categoria.
- **Arquitetura (BP-09):** `app/main.py` (5.827 linhas, 56 rotas) extraído
  para 10 módulos de domínio (`router.py`/`service.py`/`schemas.py` cada),
  sem nenhuma mudança de comportamento — ver `docs/architecture/overview.md`.
- Deploy na Vercel migrado do modelo `builds`/`routes` legado para `services`,
  com `buildCommand` rodando as migrations no build em vez de no cold start.

### Added

- **LGPD:** exclusão de conta (`DELETE /api/auth/me`), exportação de dados
  (`GET /api/privacy/export`), registro de consentimento (`consents` + aceite no
  cadastro), página pública `/privacidade` e contato do encarregado (DPO).
- **Segurança:** proteção CSRF double-submit (`X-CSRF-Token`) para sessões por cookie.
- Camada `app/core` real (`config`, `database`, `security`, `storage`) extraída do monólito.
- Fotos de perfil em Supabase Storage privado (URL assinada), com fallback em disco no dev.
- Endpoints de saúde separados: `/api/health` (readiness, com DB) e `/api/health/live` (liveness).
- Login e cadastro com identidade Trevo, demo do produto e OAuth (Google, GitHub, Facebook) preparado por variáveis de ambiente.
- Documentação reorganizada em `docs/` (produto, arquitetura, segurança, guias).
- Templates GitHub (issues, PR), `LICENSE` (MIT), `SECURITY.md`, `CONTRIBUTING.md`.

### Changed

- Configuração centralizada em `app/core/config.py` (fonte única).
- Camada de dados serverless-safe (pool em container, conexão-por-request no Vercel).
- OAuth state agora é stateless assinado (funciona entre invocações serverless).
- CI: gate de cobertura (`--cov-fail-under=60`) e `pip-audit` no lugar do `safety` (descontinuado).
- CSP endurecida (remoção de origens externas de script/fonte não usadas).
- README profissional para open source.
- `.gitignore` ampliado para artefatos Python, Node, logs e secrets.

### Security

- Remoção de `__pycache__` versionado por engano.
- Guia de secrets e rotação em `docs/security/security.md`.
