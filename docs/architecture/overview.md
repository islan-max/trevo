# Arquitetura do backend

BP-09 da auditoria técnica ([docs/auditoria-2026-09.md](../auditoria-2026-09.md))
extraiu `app/main.py` (5.827 linhas, 56 rotas) para módulos por domínio.
Refatoração estritamente mecânica: nenhuma correção de bug, nenhuma
otimização, nenhuma mudança de assinatura de rota — o `openapi.json` gerado
pelo app é byte a byte idêntico ao de antes da extração.

## Estrutura

```
app/
├── core/            config · database · security · signing · storage ·
│                    logging · startup · ephemeral (estado efêmero de processo)
├── shared/          money · dates · clock · serialization · validation
├── api/
│   ├── deps.py      get_current_user, PlainDictRoute, request_cached,
│   │                sessão/cookies/CSRF, rate limit por IP — usados por
│   │                TODOS os domínios
│   ├── middleware.py  request_id · content-type · csrf · security headers
│   └── health.py    /api/health, /api/health/live (infra, não é domínio)
├── auth/ privacy/ categories/ cards/ transactions/ budgets/ goals/ users/
│   imports/ reports/ dashboard/
│                    cada um com router.py + service.py (+ schemas.py onde
│                    há payload Pydantic próprio; repository.py só onde a
│                    separação de consulta SQL vs. regra de negócio refletia
│                    uma linha real no código — a maioria das funções deste
│                    projeto mistura as duas em poucas linhas, e forçar
│                    repository.py vazio ou artificial em todo domínio seria
│                    abstração sem função)
└── main.py          app, middlewares, include_router, lifespan (174 linhas)
```

## Regras de dependência

- **`core` e `shared` nunca importam de nenhum domínio.** Verificado por
  grep: nenhum `app/core/*.py` ou `app/shared/*.py` importa de
  `app/{auth,cards,...}/`.
- **Domínios não importam uns dos outros**, com as exceções documentadas
  abaixo — todas descobertas durante a extração, não desenhadas antes dela.
  Este é um agregador financeiro pessoal: quase todo cálculo (score,
  alertas, metas, orçamento) depende de configurações, categorias e cartões
  do MESMO usuário ao mesmo tempo, então a coesão real do domínio de negócio
  não bate exatamente com dez pastas isoladas. Documentar a exceção onde ela
  existe pareceu mais honesto do que forçar uma camada de indireção só para
  a regra de grep passar.
- **`app/api/` é infraestrutura cross-cutting, não um domínio** — pode
  importar de qualquer domínio quando a dependência é inerente ao papel dele
  (ex.: `get_current_user`, usado por toda rota autenticada, precisa
  resolver o usuário da sessão).

### Exceções (import de um domínio para outro)

| De | Para | Por quê |
|----|------|---------|
| `app/api/deps.py` | `app.auth.service` | `get_current_user` precisa de `get_user_by_id`/`public_user` para resolver a sessão — é a própria razão de existir da função. |
| `app/dashboard/` | `auth`, `budgets`, `cards`, `categories`, `goals`, `transactions`, `users` | Composição de leitura pura: `bootstrap`, `calculate_score` e `get_alerts_for_month` agregam dados de todos os módulos financeiros num payload só. Exceção prevista desde o desenho original do BP-09. |
| `app/reports/` | `budgets`, `cards`, `dashboard`, `goals`, `imports` | Mesma natureza de `dashboard` — `get_reports_summary` e o PDF de relatório reúnem os mesmos agregados; `imports` fornece `csv_safe_cell` para a exportação CSV. Exceção prevista desde o desenho original do BP-09. |
| `app/goals/` | `app.users.service` | `_compute_goals` precisa de `get_settings`/`get_effective_income` (renda configurada e renda efetiva) para calcular meta diária e orçamento disponível. |
| `app/users/` | `app.auth.service` | `get_settings` cria as configurações padrão (`ensure_user_defaults`) na primeira vez que um usuário sem `settings` é lido — o mesmo bootstrap que o cadastro já faz. |
| `app/auth/` | `app.privacy.service` | `register` grava o consentimento de termos (`record_consent`) como parte do próprio fluxo de cadastro — LGPD Art. 7/8 exige o registro no momento em que o consentimento é dado. |
| `app/imports/` | `budgets`, `categories` | `build_csv_import_preview` categoriza cada linha do extrato batendo contra as categorias do usuário e as regras de categorização já cadastradas — sem isso, toda importação cairia em "sem categoria". |

- `main.py` importa só routers (`app.*.router`) e infraestrutura de
  `app.api`/`app.core` — nunca uma função de domínio diretamente.

## Estado efêmero (`app/core/ephemeral.py`)

`card_pin_failures`, `card_unlock_sessions`, `csv_import_sessions`,
`login_failures`, `revoked_token_hashes` e `ip_rate_limit_fallback` são
fallbacks em memória de processo, usados quando o Postgres não está
disponível (ou, para revogação de token, como cache de leitura rápida antes
de consultar o banco). Em serverless cada instância tem o seu — nunca
substituem a persistência, só evitam que uma falha transitória do banco
derrube rate limit, PIN de cartão ou sessão de importação por completo.

## Validação desta extração

- `python -c "from app.main import app; import json; json.dumps(app.openapi())"`
  gerado antes e depois da extração: **idêntico**, comparado campo a campo.
- `ruff check .`: limpo.
- Suíte de testes local (sem Postgres): 102 passed, mesma contagem de antes
  da extração — nenhum teste unitário mudou de comportamento.
- Note sobre `app.routes`: esta versão do FastAPI usa roteamento preguiçoso
  para routers incluídos via `include_router` (`_IncludedRouter`, que só
  expõe a lista de rotas de verdade em `.original_router.routes`). Isso
  significa que `len(app.routes)` sozinho não é mais um proxy confiável do
  número de rotas registradas — a validação de fato é o diff do
  `openapi.json`, que resolve as rotas de verdade independente de como o
  FastAPI as guarda internamente.
