# Auditoria técnica e plano de evolução — Trevo

*Auditoria independente conduzida em 15 de setembro de 2026 sobre o commit `b9d368e` (branch `main`). Toda afirmação marcada **CONFIRMADO** tem evidência direta de código, de comando executado ou de build. Nada aqui foi implementado — esta etapa é diagnóstico e plano.*

**Comandos executados para diagnóstico:** `ruff check .` (ruff 0.16.6 — passou), `pytest tests/ -q -rs` (80 passaram, 28 pulados por falta de `TEST_DATABASE_URL`), `tsc --noEmit` (limpo), `npm run test:money` (3 passaram), `next build` (sucesso, tabela de bundle na seção 11), inspeção de `requirements-lock.txt` e verificação em runtime do backend de armazenamento do `slowapi`.

---

# 1. Executive Summary

O Trevo é um produto real, coerente e acima da média para seu porte — e é também um sistema com problemas estruturais que não aparecem em nenhuma métrica verde.

**O que está bom, de verdade.** A modelagem usa chaves estrangeiras compostas `(user_id, id)` que tornam o vazamento entre usuários impossível no nível do banco. Dinheiro é `Decimal` em todo o backend, com distribuição exata de centavos. A validação é rigorosa: todo payload é Pydantic com `extra = "forbid"` e limites de tamanho. O design system é token-driven com contraste auditado e documentado. Não há um único `TODO`, `console.log`, `@ts-ignore` ou `any` no código. `tsc --noEmit` e `ruff check .` passam limpos. Isso é disciplina genuína.

**O que está quebrado e ninguém viu.** Três defeitos de impacto alto que a auditoria anterior não encontrou:

1. **O cache de respostas do frontend não é limpo no logout.** A chave do cache não contém identidade do usuário (é sempre a constante `__trevo_cookie_session__`), o logout é navegação client-side e o estado do módulo sobrevive. Usuário A sai, usuário B entra na mesma aba, e vê o painel financeiro de A por até 30 segundos.
2. **O rate limiting por IP não existe em produção.** Verifiquei em runtime: o `slowapi` está com `MemoryStorage`. Em serverless cada instância tem o próprio contador. Cadastro, troca de senha, exclusão de conta e exportação de dados estão, na prática, sem limite.
3. **A importação de CSV em modo "substituir" apaga lançamentos que não vieram do arquivo.** Ela deleta *tudo* nos meses do CSV — manuais, parcelas de cartão, recorrentes — e mutila grupos de parcelas pela metade.

**O que é dívida arquitetural.** `app/main.py` continua com 5.133 linhas e as oito pastas de domínio continuam com um `__init__.py` de uma linha. A consequência prática é que cada correção acima exige mexer no mesmo arquivo, e não existe fronteira onde testar um domínio isoladamente.

**O que está superdimensionado.** Um gerador de PDF escrito byte a byte (≈420 linhas). A biblioteca `supabase` inteira (8+ pacotes transitivos) para assinar URL de avatar. `lucide-react` com zero importações. Dependências de teste (`pytest`, `ruff`, `bandit`, `factory-boy`, `freezegun`) dentro de `requirements.txt`, ou seja, dentro do bundle de produção.

**Veredito.** O Trevo não precisa de reescrita, nem de microserviços, nem de fila, nem de cache distribuído. Precisa de correções de segurança e integridade de dados (BP-01 a BP-03), depois CSV e performance (BP-04, BP-06), e então a extração de roteadores — que é o que torna todo o resto sustentável. O restante é limpeza.

---

# 2. Architecture Map

## Como o sistema realmente funciona hoje

```
┌─────────────────────────────────────────────────────────────────┐
│  Navegador                                                       │
│  Next.js 15 App Router, output: "export" (18 páginas estáticas)  │
│  Sessão = cookie HttpOnly. O token NUNCA vai para localStorage.  │
│  lib/api.ts: cliente único, cache GET de 30 s, CSRF double-submit│
└───────────────────────┬─────────────────────────────────────────┘
                        │ fetch(credentials: "include")
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│  Vercel (deploy primário)                                        │
│  vercel.json:                                                    │
│    /api/*  → api/index.py  (@vercel/python, ASGI)                │
│    /*      → frontend/out/ (@vercel/static-build)                │
│  Mesma origem ⇒ ALLOWED_ORIGINS e NEXT_PUBLIC_API_BASE_URL vazios│
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│  app/main.py — 5.133 linhas, 56 rotas                            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ 4 middlewares: request_id → content-type → CSRF → headers │  │
│  │ PlainDictRoute (desliga response_model p/ Decimal virar nº)│  │
│  │ request_cached (ContextVar, escopo de request)            │  │
│  │ ensure_serverless_schema (migra no 1º request do processo)│  │
│  ├───────────────────────────────────────────────────────────┤  │
│  │ rotas + regras de negócio + SQL + PDF, tudo no mesmo nível │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                  │
│  app/core/     config, database, security, signing, storage,     │
│                logging   ·  errors.py e middleware.py: MORTOS    │
│  app/shared/   money, dates                (usados)              │
│  app/integrations/ normalizer (usado) · base.py: MORTO           │
│  app/privacy/  service (LGPD)              (usado)               │
│  app/oauth.py  Google/GitHub/Facebook, state stateless assinado  │
│  app/auth|transactions|budgets|cards|categories|dashboard|       │
│      goals|imports|reports|users/  ← 8 pastas com __init__ vazio │
└───────────────────────┬─────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────┐
│  PostgreSQL (Supabase, us-east-2)                                │
│  16 tabelas. Serverless: 1 conexão NOVA por db_cursor().         │
│  Container: pool ThreadedConnectionPool(2,10).                   │
│  Schema = SCHEMA_SQL idempotente em migrate.py  +  migrations/   │
│           (duas fontes de verdade; 0007 não existe)              │
└─────────────────────────────────────────────────────────────────┘
```

## Três alvos de deploy, um deles morto

| Alvo | Config | Estado |
|---|---|---|
| **Vercel** (primário) | `vercel.json`, `api/index.py` | Funcional. Sem passo de build que rode migrations — elas rodam no primeiro request de cada processo. |
| **Docker / Railway** | `Dockerfile` multi-stage, `railway.json` | Funcional. Instala `requirements-lock.txt`; o FastAPI serve `frontend/out`. |
| **Render** | `render.yaml` | **Quebrado.** `env: python`, `buildCommand: pip install -r requirements.txt` — nunca constrói o frontend, então `FRONTEND_OUT_DIR` não existe e o app responde só a API. O nome do serviço ainda é `ritmo-financeiro-pro` (terceira marca, anterior a Pulsa/Trevo). |

## O contrato de dinheiro

Decisão boa e frágil ao mesmo tempo: `PlainDictRoute` ([app/main.py:184](app/main.py:184)) desliga o `response_model` de todas as rotas porque o Pydantic v2 serializaria `Decimal` como string e o frontend declara `number`. Funciona, está documentado no código e tem teste (`tests/unit/test_serialization.py`). O custo é que **nenhuma rota tem schema de resposta validado** — o OpenAPI em `/docs` não descreve nada útil e uma mudança de formato só aparece em runtime.

---

# 3. Previous Audit Reconciliation

A auditoria anterior (`docs/auditoria-tecnica.md`) é honesta e, na maioria, correta. Ela também se corrige em público uma vez, o que é bom sinal. Abaixo, cada afirmação confrontada com o código atual.

| # | Item da auditoria anterior | Estado atual | Evidência | Ação |
|---|---|---|---|---|
| 1 | Duplicação entre `app/shared`/`app/integrations` e `main.py`; consolidada | **Resolvido** | [app/main.py:60-90](app/main.py:60) importa `distribute_installments`, `round_money`, `format_brl`, `to_decimal`, `build_duplicate_hash`, `parse_decimal_text`, `first_billing_month`. Nenhuma cópia local. | Nenhuma |
| 2 | `main.py` monolítico ≈5.000 linhas; pastas de domínio vazias | **Confirmado — persiste** | `wc -l app/main.py` = 5.133. `app/auth/__init__.py` … `app/users/__init__.py` = 1 linha cada. | BP-09 |
| 3 | FK composta `(user_id, id)` torna vazamento impossível no banco | **Parcialmente correto** | Verdadeiro para `transactions`, `budgets`, `categorization_rules`, `card_pin_failures_state`, `card_unlock_sessions_state`. **Falso para `card_pins`** ([migrate.py:110](migrate.py:110)): referencia `cards(id)` e `users(id)` separadamente. É a única tabela que rompe a invariante elogiada. | BP-08 |
| 4 | Dia de fechamento ignorado na criação de parcelas; corrigido | **Parcialmente corrigido** | `create_installments` ([app/main.py:4850](app/main.py:4850)) chama `first_billing_month`. `create_transaction` ([app/main.py:4065](app/main.py:4065)) **não chama** — uma compra avulsa no cartão depois do fechamento continua caindo na fatura errada. | BP-03 |
| 5 | Juros de parcelamento usam saldo médio; "não é bug de implementação" | **Refutado em parte** | Há um bug concreto: `round_money(Decimal(payload.interestRate) / 100)` ([app/main.py:4909](app/main.py:4909), [:4954](app/main.py:4954)) quantiza a **taxa** para 2 casas. 1,99 % a.m. vira 2 %; 0,4 % vira **0 %** e os juros somem. Além disso `Decimal(float)` contraria o `to_decimal(str(...))` usado no resto do código. | BP-03 |
| 6 | Camada de segurança acima da média | **Confirmado, com ressalva grave** | JWT HttpOnly, CSRF double-submit, revogação por hash, `iat` vs `password_changed_at`, bcrypt dummy contra enumeração, logs com e-mail/IP hasheados — tudo presente. **Mas** a camada de rate limit por IP é `MemoryStorage`, inoperante em serverless (verificado em runtime). | BP-01 |
| 7 | Migrations nunca rodavam em produção; corrigido com advisory lock + verificador serverless | **Parcialmente corrigido** | `run_migrations_locked` e `ensure_serverless_schema` existem e funcionam ([app/main.py:306](app/main.py:306), [migrate.py:250](migrate.py:250)). **`vercel.json` continua sem qualquer passo de build** que execute migrations. E a solução faz `SCHEMA_SQL` inteiro rodar a cada cold start — incluindo `ALTER TABLE ... DROP/ADD CONSTRAINT` em `transactions`, que pega `ACCESS EXCLUSIVE`. | BP-08, BP-11 |
| 8 | `schema_migrations` dessincronizado; 0008–0012 aplicadas | **Não verificável + achado novo** | Sem acesso ao banco de produção nesta auditoria. No repositório, **a migration `0007` não existe** e `git log --diff-filter=D -- migrations/` não mostra remoção. Ou foi escrita e perdida, ou a numeração pulou. | Investigar (BP-08) |
| 9 | Rebrand para Trevo concluído | **Resolvido no produto; resíduos na infra** | Paleta, fontes, ícones e tokens, sim. Mas `render.yaml:3` = `ritmo-financeiro-pro`; `docker-compose.yml` usa usuário/banco `ritmo`; `CategoryPayload.color` ([app/main.py:2679](app/main.py:2679)) ainda tem default `#9be768` — o verde da marca **anterior**. | BP-10 |
| 10 | Degradê branco no card principal (`via-ink`, 1,15:1) | **Resolvido** | `frontend/app/globals.css`: `--hero-from/--hero-via/--hero-to` escuros nos dois temas, com o raciocínio no comentário. | Nenhuma |
| 11 | Dark mode excessivamente verde | **Resolvido** | `:root[data-theme="dark"]` usa cinzas frios; verde só como acento. | Nenhuma |
| 12 | `bg-leaf` + branco no estado ativo (3,45:1 / 2,48:1) | **Resolvido** | `--selected-bg`/`--selected-fg` por tema, com o cálculo documentado no CSS. | Nenhuma |
| 13 | Paleta de gráficos com roxo/rosa herdados; eixo dividindo tudo por mil | **Resolvido** | `frontend/components/charts/chartTheme.ts` existe e centraliza. | Nenhuma |
| 14 | Estado de carregamento fingindo R$ 0,00 | **Resolvido** | `frontend/components/Skeleton.tsx` e `ChartSkeleton.tsx` com `aria-busy`. | Nenhuma |
| 15 | Autocorreção: skip link, `aria-current` e marcos já existiam | **Confirmado — a autocorreção estava certa** | [AppShell.tsx:96](frontend/components/AppShell.tsx:96) tem "Pular para o conteúdo"; `BottomNav`/`SidebarItem` têm `aria-current`. | Nenhuma |
| 16 | recharts sob demanda: primeira tela de 244 → 130 kB | **Confirmado no dashboard; pendência segue aberta** | Build atual: `/dashboard` = **130 kB**. `/relatorios` = **240 kB**, porque [app/relatorios/page.tsx:5](frontend/app/relatorios/page.tsx:5) importa `recharts` estaticamente. A própria auditoria listou isso como pendente. | BP-07 |
| 17 | N+1 consolidados; cache de escopo de request | **Parcial** | Consolidado: série de 12 meses ([app/main.py:1730](app/main.py:1730)) e alertas por categoria. **Não consolidado:** `get_cards_summary` ([:1152](app/main.py:1152)) faz 2 queries por cartão + 1 por grupo de parcelas + `get_card_commitment` com conexão própria; `calculate_score` ([:2346](app/main.py:2346)) e `get_alerts_for_month` ([:2375](app/main.py:2375)) fazem `get_invoice_total` por cartão. `request_cached` cobre apenas `goals` e `budget_summary` — `get_cards_summary` roda **duas vezes** dentro de `/api/bootstrap`. | BP-06 |
| 18 | Índice de expressão com `INCLUDE (amount, type)` | **Confirmado** | `migrations/0012_transaction_month_index.sql`. Expressão idêntica à usada nas queries. | Nenhuma |
| 19 | Testes pulados silenciosamente; aviso + gate de CI | **Resolvido** | `pytest_report_header` em [tests/conftest.py:21](tests/conftest.py:21); passo "Falhar se algum teste foi pulado" em `ci.yml`. Verificado: 28 skips aparecem no relatório local. | Nenhuma (ver CI-01) |
| 20 | `ruff` sem configuração própria; CI quebraria sozinho | **Resolvido** | `ruff.toml` com conjunto de regras explícito e os dois descartes justificados. `ruff check .` passa com ruff 0.16.6. | Nenhuma |
| 21 | Incidente `app/core/secrets.py` engolido pelo `.gitignore`; verificador no CI | **Resolvido, mas incompleto** | `scripts/check-ignored-files.sh` existe e roda no CI. Porém só verifica `.py|.ts|.tsx|.sql|.css` — e o `.gitignore` ignora **`*.csv` e `*.pdf`**, exatamente os formatos dos dois recursos centrais. Uma fixture de extrato bancário seria engolida pela mesma armadilha. | BP-00 |
| 22 | OAuth sem credenciais nos provedores | **Confirmado + achado novo** | Código implementado e testado (`tests/unit/test_oauth.py`). Achado novo: `resolve_oauth_user` ([app/main.py:944](app/main.py:944)) **vincula silenciosamente** a identidade social a uma conta e-mail/senha existente que casa por e-mail, sem confirmação de senha. | BP-05 |
| 23 | Segredo de assinatura provisionado em `app_secrets` | **Implementado como descrito; trade-off não avaliado** | `app/core/signing.py` funciona. Mas o segredo fica **em texto claro** numa tabela. O comentário da migration 0011 argumenta que "quem alcança esta tabela já alcança as demais" — verdade para leitura de dados, falso para **forjar sessão de qualquer usuário**, inclusive por backup, dump, painel do Supabase ou service-role key vazada. | BP-05 |
| 24 | Open Finance é espaço reservado; caminho viável é agregador licenciado | **Confirmado** | `app/integrations/base.py` existe e **não é importado por ninguém**. `app/integrations/open_finance/README.md` cita um `TransactionNormalizer` que não existe. A análise regulatória está correta. | BP-12 |
| 25 | Lighthouse a reexecutar; LCP em aberto | **Continua em aberto** | Sem medição nova. O bundle foi medido e confirma a redução do dashboard. | BP-07 |

**Onde a auditoria anterior errou ou foi incompleta:**

- Tratou a garantia de isolamento por FK composta como universal. Não é: `card_pins` é a exceção.
- Classificou os juros como "imprecisão da fórmula, não bug". Há um bug de implementação real e separado.
- Declarou os N+1 "consolidados". Os mais caros continuam lá, e o endpoint mais chamado executa o pior deles duas vezes.
- Declarou o problema de migrations "corrigido em três frentes". A causa raiz apontada — `vercel.json` sem passo de build — **não foi corrigida**; foi contornada em runtime, ao custo de DDL pesado a cada cold start.
- Descreveu o rate limiting como "duas camadas". Uma delas não funciona no alvo de deploy primário.
- Não tocou em fusos horários, no vazamento de cache entre usuários, no modo "substituir" da importação, nem na colisão de `installment_group`.

---

# 4. Strengths

Coisas que devem ser **preservadas** e que tornam o resto do plano viável.

| Força | Evidência | Por que importa |
|---|---|---|
| **Isolamento multiusuário no banco** | FKs compostas `FOREIGN KEY (user_id, category_id) REFERENCES categories(user_id, id)` em `transactions`, `budgets`, `categorization_rules` | Um bug de aplicação não consegue cruzar dados entre usuários. Raro nesse porte. |
| **Dinheiro é `Decimal`, ponta a ponta** | `app/shared/money.py`; `round_money` com `ROUND_HALF_UP`; `distribute_installments` trabalha em centavos inteiros e fecha a diferença na última parcela | Nenhum erro de ponto flutuante em valores monetários. |
| **Sessão sem token no cliente** | `frontend/lib/authSession.ts` guarda apenas um *hint* booleano; o token vive só no cookie `HttpOnly` | XSS no frontend não rouba a sessão. Muitos projetos maiores erram isso. |
| **Validação de entrada rigorosa e uniforme** | 22 modelos Pydantic, todos com `extra = "forbid"`, `min_length`/`max_length`, `ge`/`le`, `Literal` para enums | Superfície de injeção e de payload inesperado é pequena. |
| **Logs de auditoria sem PII em claro** | `email_hash()` e `client_ip_hash()` ([app/main.py:152](app/main.py:152)) | Logs vazados não expõem titulares. |
| **Design system com contraste calculado e justificado** | `frontend/app/globals.css` — cada token vem com o motivo e a razão de contraste no comentário | O sistema resiste a mudanças futuras porque o raciocínio está escrito. |
| **Padrão de diálogo acessível** | `ImportConfirmDialog.tsx`: `role="dialog"`, `aria-modal`, focus trap, `Escape`, restauração de foco | É um padrão pronto para replicar nos outros drawers. |
| **Higiene de código** | Zero `TODO`/`FIXME`/`console.log`/`@ts-ignore`/`any`; `tsc --noEmit` e `ruff check .` limpos | Não há ruído escondendo sinal. |
| **Comentários que explicam *por que*** | `PlainDictRoute`, `as_utc_datetime`, `first_billing_month`, `migrations/0012` | Reduz drasticamente o custo de retomar o código. |
| **Guarda contra `.gitignore` silencioso** | `scripts/check-ignored-files.sh` no CI, nascido de um incidente real | Correção estrutural, não pontual. |

---

# 5. Critical Findings (P0 / P1)

Cada achado traz: evidência, comportamento observado, consequência e classificação.

## P0 — bloqueador, segurança ou perda/corrupção de dados

---

### SEC-01 — Cache do frontend vaza dados financeiros entre usuários na mesma aba · **CONFIRMADO** · P0

**Arquivos:** [frontend/lib/api.ts:31](frontend/lib/api.ts:31), [frontend/lib/api.ts:57](frontend/lib/api.ts:57), [frontend/lib/authSession.ts:13](frontend/lib/authSession.ts:13), [frontend/app/configuracoes/page.tsx:49](frontend/app/configuracoes/page.tsx:49)

**Evidência.** O cache é um `Map` de módulo:

```ts
const responseCache = new Map<string, CacheEntry>();
function cacheKey(path: string, token?: string | null) {
  return `${token || "public"}::${path}`;
}
```

O `token` passado por toda a aplicação é a **constante** `COOKIE_AUTH_TOKEN = "__trevo_cookie_session__"` — a sessão real está no cookie. Portanto a chave é idêntica para qualquer usuário: `__trevo_cookie_session__::/api/bootstrap?month=2026-09`.

O logout (`endSession`) faz `api.logout(token)` → `clearSession()` → `router.replace("/login")`. `clearSession()` mexe apenas em `localStorage`/`sessionStorage`. `router.replace` é navegação **client-side**: o módulo `lib/api.ts` não é recarregado e `responseCache` sobrevive intacto.

**Comportamento.** Usuário A faz logout. Usuário B faz login na mesma aba. Nos primeiros 30 segundos (`GET_CACHE_TTL_MS = 30_000`), qualquer `GET` que A tenha feito devolve a resposta de A: saldo, transações, cartões, score, orçamento.

**Consequência.** Exposição de dados financeiros pessoais a terceiro em dispositivo compartilhado — computador de família, lan house, máquina de escritório. Para um produto financeiro sob LGPD, é incidente de segurança, não bug de UX.

**Correção mínima:** exportar `clearApiCache()` de `lib/api.ts`, chamá-la em `clearSession()` e após login/registro bem-sucedidos. Correção mais robusta: incluir o `id` do usuário autenticado na chave do cache.

---

### SEC-02 — Rate limiting por IP é inoperante em produção · **CONFIRMADO** · P0

**Arquivos:** [app/main.py:187](app/main.py:187) e todas as rotas com `@limiter.limit`

**Evidência.** Executei em runtime:

```
>>> Limiter(key_func=get_remote_address)._storage → MemoryStorage
>>> storage_uri → None
```

O `slowapi` sem `storage_uri` usa memória do processo. Rotas afetadas:

| Rota | Limite declarado | Limite real em serverless |
|---|---|---|
| `POST /api/auth/register` | 3/hora | nenhum efetivo |
| `POST /api/auth/change-password` | 3/hora | nenhum efetivo |
| `DELETE /api/auth/me` | 5/hora | nenhum efetivo |
| `GET /api/privacy/export` | 10/hora | nenhum efetivo |
| `GET /api/export/csv` | 20/hora | nenhum efetivo |
| `POST /api/auth/login` | 5/15 min | nenhum efetivo — **mas** há a camada por e-mail persistida em `login_failures_state`, que funciona |

Há um segundo problema empilhado: `get_remote_address` lê `request.client.host`. Atrás do proxy da Vercel isso é o endereço do proxy, não o do usuário — nenhum `ProxyHeadersMiddleware` nem `TrustedHostMiddleware` está montado. Mesmo com storage compartilhado, a chave seria a errada.

**Consequência.** Cadastro em massa (13 usuários reais hoje, sem barreira contra milhares), abuso de exportação de dados (endpoint caro que varre `information_schema` e dumpa tudo), força bruta de troca de senha. `POST /api/imports/csv/upload` sequer tem `@limiter.limit` e grava até 1 MB de JSONB por chamada.

**Correção mínima:** mover os contadores para Postgres (o padrão de `login_failures_state` já existe e funciona), ou aceitar que o limite por IP é decorativo e remover as anotações para não criar falsa sensação de proteção. A primeira opção é a certa.

---

### DATA-01 — Importação "substituir" apaga lançamentos que não vieram do arquivo · **CONFIRMADO** · P0

**Arquivo:** [app/main.py:3598](app/main.py:3598)

**Evidência.**

```sql
DELETE FROM transactions
WHERE user_id = %s
  AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
```

Não há filtro por `source`. O comentário no código diz que o escopo é "os lançamentos dos meses presentes no arquivo — e só deles", o que é verdade sobre *meses*, mas não sobre *origem*.

**Comportamento.** Importar o extrato do Nubank de setembro apaga, nesse mês: o aluguel lançado à mão, a parcela 4/12 da geladeira, a mensalidade recorrente da academia. A API devolve `replaced: N`, e o diálogo avisa `existingInMonths` — mas o usuário lê "vou substituir o que importei antes", não "vou apagar o que digitei".

**Consequência agravada nas parcelas.** Deletar a parcela 4/12 de um `installment_group` deixa o grupo com 11 linhas e um buraco. `get_grouped_installment_purchases` agrega com `MAX(total_installments)` e `COUNT(*)`, então a compra passa a aparecer como "12 parcelas, 11 restantes" para sempre. A projeção de fatura e o `committedLimit` ficam errados permanentemente, e não há caminho de recuperação na interface.

**Correção mínima:** restringir o `DELETE` a `source = 'csv_import'` e a lançamentos sem `installment_group`. Alternativa mais honesta: substituir o modo "replace" por "desfazer última importação" (por lote), que é o que o usuário realmente quer.

---

## P1 — defeito grave ou risco estrutural

---

### PERF-01 — Importação de CSV grande estoura o timeout da função · **CONFIRMADO** · P1

**Arquivos:** [app/main.py:2930](app/main.py:2930) (`resolve_import_category`), [app/main.py:2950](app/main.py:2950) (`build_csv_import_preview`), [app/main.py:3588](app/main.py:3588) (`confirm_csv_import`)

**Evidência.** `resolve_import_category` é chamada **uma vez por linha** do arquivo, e internamente:

```python
for category in list_categories(user_id):      # 1 query ao banco
...
rule = match_categorization_rule(user_id, description)   # +1 query ao banco
```

Nenhuma das duas é `request_cached`. Com o teto de `CSV_IMPORT_MAX_ROWS = 5000`, isso é até **10.000 idas ao banco** para montar um preview. Em serverless, cada `db_cursor()` abre **uma conexão nova** ([app/core/database.py:58](app/core/database.py:58)).

Pior: `confirm_csv_import` chama `build_csv_import_preview` de novo (recomputando tudo) e **depois** entra no laço de inserção, que faz mais 1 `SELECT` de duplicata por linha, tudo dentro de uma transação aberta.

**Consequência.** Um extrato anual típico (1.500–3.000 linhas) não completa dentro do limite de execução da função. O usuário vê o upload funcionar, o preview travar ou falhar, e não há retomada.

**Correção:** carregar categorias e regras uma vez por importação (dicionário em memória), e trocar o `SELECT` de duplicata por linha por um `INSERT ... ON CONFLICT (user_id, duplicate_hash) DO NOTHING` em lote, usando o índice único que já existe.

---

### DOM-01 — `installment_group` é chave natural derivada de texto do usuário · **CONFIRMADO** · P1

**Arquivos:** [app/main.py:4850](app/main.py:4850), [app/main.py:4959](app/main.py:4959)

**Evidência.**

```python
group = f"{user_id}-{card_id}-{title}-{purchase_date}"          # com cartão
group = f"{user_id}-installment-{title}-{purchase_date}"        # sem cartão
```

**Comportamento.** Duas compras de mesmo título, mesmo cartão e mesma data produzem a **mesma** string de grupo. Cenários reais: "Passagem aérea" comprada duas vezes no mesmo dia (ida e volta separadas), "Notebook" de dois membros da família, ou simplesmente o usuário repetindo a operação por achar que a primeira falhou.

Consequências encadeadas:
- As 12 linhas da segunda compra entram no grupo da primeira. `installment_number` fica duplicado (dois registros com `1`, dois com `2`, …).
- `create_installments` devolve `createdInstallments: 24` porque relê o grupo inteiro — o usuário vê um número que não corresponde à sua ação.
- `get_grouped_installment_purchases` soma as duas compras como uma só, com `MAX(total_installments) = 12` e `COUNT(*) = 24`.
- `delete_transaction` de qualquer linha apaga **as duas compras**, sem aviso.

**Correção:** `installment_group` deve ser um UUID gerado no servidor. A migração de dados existentes é opcional (dados atuais permanecem consistentes se não houver colisão já ocorrida — vale um `SELECT installment_group, COUNT(DISTINCT installment_number) …` de verificação antes).

---

### DOM-02 — Taxa de juros é arredondada para centavos antes de ser aplicada · **CONFIRMADO** · P1

**Arquivos:** [app/main.py:4909](app/main.py:4909), [app/main.py:4954](app/main.py:4954)

**Evidência.**

```python
interest_rate = round_money(Decimal(payload.interestRate) / Decimal("100"))
```

`round_money` quantiza para `0.01`. A variável não é dinheiro, é uma **taxa**.

| Taxa informada | Após `round_money` | Erro |
|---|---|---|
| 1,99 % a.m. | 2,00 % | superestima |
| 2,49 % a.m. | 2,00 % | subestima 20 % |
| 0,40 % a.m. | **0,00 %** | juros desaparecem |
| 0,99 % a.m. | 1,00 % | — |

Além disso `Decimal(payload.interestRate)` constrói `Decimal` a partir de `float`, o anti-padrão que `to_decimal()` (que usa `str()`) existe justamente para evitar — e que está em uso em todo o resto do código.

**Consequência.** A simulação de compra parcelada — que é uma das funcionalidades vendidas no README — mostra um número que não corresponde à taxa digitada. Em taxas abaixo de 0,5 % a.m. (consórcio, parcelamento promocional) o produto afirma que não há juros.

**Nota sobre o escopo.** A auditoria anterior classificou a fórmula de saldo médio como imprecisão aceitável. Concordo — a fórmula é uma escolha de produto. Mas o arredondamento da taxa é um defeito de implementação independente e deve ser corrigido mesmo que a fórmula permaneça.

---

### DOM-03 — Compra avulsa no cartão ignora o dia de fechamento · **CONFIRMADO** · P1

**Arquivo:** [app/main.py:4065](app/main.py:4065)

**Evidência.** `create_transaction` insere `billing_month` exatamente como veio no payload (`validate_month_text(payload.billingMonth)`, que devolve `None` se ausente). Nunca chama `first_billing_month`, apesar de aceitar `cardId`.

**Comportamento.** Cartão com fechamento no dia 20. Compra no crédito no dia 25 de setembro. Sem `billingMonth` explícito, `billing_month` fica `NULL` e o `COALESCE` cai no mês da data: setembro. A fatura correta é a de outubro.

**Consequência.** Exatamente o mesmo defeito que a auditoria anterior corrigiu para parcelamentos, ainda vivo para compras à vista no crédito — que são a maioria. Todas as consequências derivadas seguem: `get_invoice_total` errado, `availableCredit` errado, alerta de 80 % do limite disparando no mês errado, `calculate_score` penalizando o mês errado.

---

### DOM-04 — Todo o conceito de "hoje" e "mês atual" é calculado em UTC · **CONFIRMADO** · P1

**Arquivos:** [app/shared/dates.py:30](app/shared/dates.py:30) (`get_current_month`), [app/main.py:1864](app/main.py:1864) (`_compute_goals`)

**Evidência.**

```python
def get_current_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")
...
today = datetime.now(UTC).date()
```

**Comportamento.** O produto é pt-BR, mobile-first, e o horário de Brasília é UTC−3. Das 21h00 à meia-noite, o servidor já está no dia seguinte:

- `progress_day` avança um dia cedo demais → `currentAverageSpend` e `projectedClosing` são calculados com um divisor maior, e a meta diária cai artificialmente.
- No dia 30 de setembro às 21h30 BRT, `get_current_month()` devolve `2026-10`. Toda tela que não passa `month` explícito muda de mês três horas antes da virada.
- `availableToday` no dashboard divide por `totalDays - progressDay + 1`, que também desloca.

**Consequência.** O número central do produto — "quanto posso gastar hoje" — está errado durante três horas todo dia, e o mês exibido troca cedo na virada. É o tipo de bug que gera reclamação difusa ("os números dançam à noite") e é invisível em teste.

**Correção:** introduzir um fuso do usuário (ou fixar `America/Sao_Paulo` enquanto o produto for só pt-BR) e derivar `today`/`current_month` dele. `ruff` está com a regra `DTZ` ativa, então a correção não regride.

---

### DOM-05 — Salário pode ser contado duas vezes · **CONFIRMADO** · P1

**Arquivos:** [app/main.py:1800](app/main.py:1800) (`get_dashboard`), [app/main.py:1930](app/main.py:1930) (`_compute_goals`), [app/main.py:2300](app/main.py:2300) (`calculate_score`)

**Evidência.** Três fórmulas independentes somam a mesma coisa:

```python
balance          = base_income + inflow - outflow          # base_income = settings.monthly_income
available_budget = monthly_income + inflow - reserve_amount
denominator      = monthly_income + inflow                 # no score
```

`inflow` é a soma de **todas** as transações de tipo `income` do mês. `monthly_income` é um campo de configuração.

**Comportamento.** Nada impede — nem avisa — que o usuário registre o salário como lançamento de entrada, que é o gesto natural para quem quer ver o extrato completo. Quando faz isso, o orçamento disponível dobra, a meta diária dobra, o Ritmo Score infla e os alertas param de disparar.

**Consequência.** O produto responde à pergunta central com um número que pode estar 100 % acima do real, sem nenhum sinal. Isso é ambiguidade de domínio, não bug de código: falta decidir se `settings.monthly_income` é *renda esperada* (usada só quando não há lançamento) ou *renda base garantida* (somada sempre).

**Correção recomendada:** transformar `monthly_income` em renda esperada e calcular `renda efetiva = max(monthly_income, inflow)` — ou, melhor, marcar uma categoria/lançamento como "salário" e deduplicar por ele. A decisão é de produto; a auditoria só registra que hoje ela não foi tomada e o código assume a opção mais perigosa.

---

### SEC-03 — Criar categoria com nome existente reescreve o tipo da categoria antiga · **CONFIRMADO** · P1

**Arquivo:** [app/main.py:4005](app/main.py:4005)

**Evidência.**

```sql
INSERT INTO categories (...) VALUES (...)
ON CONFLICT (user_id, name)
DO UPDATE SET type = EXCLUDED.type, color = ..., icon = ..., is_active = TRUE, updated_at = NOW()
```

**Comportamento.** O usuário tem "Mercado" (expense) com 200 lançamentos históricos. Cria uma categoria "Mercado" do tipo income (por engano, ou porque não viu que já existia — a categoria pode estar arquivada e portanto invisível). O `DO UPDATE` muda o `type` da linha existente.

**Consequência.** Os 200 lançamentos de despesa passam a pertencer a uma categoria de receita. Toda tela que agrupa por tipo de categoria quebra: seletor de categorias no formulário de despesa deixa de oferecê-la, `get_budget_summary` deixa de considerá-la, o gráfico de pizza de despesas perde a fatia. O histórico fica visualmente corrompido e não há undo.

**Correção:** o upsert deve ser usado **apenas** para reativar categorias arquivadas (`WHERE is_active = FALSE`), e nunca alterar `type` de uma categoria com lançamentos. Categoria ativa com o mesmo nome deve responder 409.

---

### OPS-01 — DDL pesado a cada cold start em serverless · **CONFIRMADO** · P1

**Arquivos:** [app/main.py:306](app/main.py:306) (`ensure_serverless_schema`), [migrate.py:16-190](migrate.py:16) (`SCHEMA_SQL`), [migrate.py:250](migrate.py:250) (`run_migrations_locked`)

**Evidência.** `ensure_serverless_schema` roda uma vez por processo, no primeiro request de `/api/`, e chama `run_migrations_locked` → `run_migrations` → `cursor.execute(SCHEMA_SQL)`. O `SCHEMA_SQL` tem ~175 linhas e inclui:

```sql
CREATE OR REPLACE FUNCTION set_updated_at() ...
DROP TRIGGER IF EXISTS users_set_updated_at ON users;  CREATE TRIGGER ...
ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_source_check;
ALTER TABLE transactions ADD CONSTRAINT transactions_source_check CHECK (...);
```

Isso tudo sob `pg_advisory_lock`, a cada processo novo.

**Consequência.**
1. **Latência de cold start.** O primeiro request de cada instância paga um roundtrip de DDL antes de responder. Em serverless, instâncias nascem o tempo todo.
2. **Lock exclusivo em `transactions`.** `ALTER TABLE ... ADD CONSTRAINT` pega `ACCESS EXCLUSIVE` e revalida a tabela inteira. Em instâncias paralelas, uma segura o advisory lock e as outras esperam.
3. **Janela sem constraint.** Entre o `DROP CONSTRAINT` e o `ADD CONSTRAINT` a checagem de `source` não existe. Improvável de ser explorado, mas é uma invariante que pisca.
4. **Duas fontes de verdade.** `login_failures_state` é criada tanto em `SCHEMA_SQL` quanto em `migrations/0005`. `is_active` e `updated_at` de `categories` aparecem em `SCHEMA_SQL` e em `0006`/`0010`. O baseline nunca foi congelado — ele cresce junto com as migrations.

**Correção:** congelar `SCHEMA_SQL` num `migrations/0000_baseline.sql` aplicado uma única vez, e fazer `ensure_serverless_schema` apenas *verificar* `schema_migrations` (um `SELECT`), não aplicar. Aplicar via passo de build (ver BP-11).

---

### SEC-04 — Vinculação OAuth silenciosa a conta de e-mail/senha existente · **CONFIRMADO** · P1

**Arquivo:** [app/main.py:944](app/main.py:944)

**Evidência.**

```python
by_email = get_user_by_email(email)
if by_email:
    ...
    UPDATE users SET auth_provider = %s, oauth_subject = %s ... WHERE id = %s
    # e devolve o usuário → login concluído
```

Não há confirmação de senha, nem e-mail de verificação, nem sequer um aviso.

**Comportamento.** Quem controlar uma conta em qualquer dos três provedores com o e-mail da vítima entra diretamente na conta financeira dela. Hoje os três provedores verificam e-mail (`_google_profile` checa `email_verified` explicitamente; `_github_profile` checa `verified` no caminho secundário; o Facebook verifica do lado dele), então o risco imediato é baixo. **O problema é o padrão**: a segurança da conta financeira passou a depender inteiramente da política de verificação de terceiros, sem defesa em profundidade e sem que essa dependência esteja escrita em lugar nenhum. Um quarto provedor adicionado sem essa checagem vira takeover imediato.

**Correção:** exigir autenticação existente antes de vincular (fluxo "vincular conta" a partir do perfil, já logado), ou pelo menos exigir a senha quando a conta tem `hashed_password` e ainda não tem `oauth_subject`.

**Limitação de produto relacionada:** `users` tem um único par `(auth_provider, oauth_subject)`, então nenhum usuário pode vincular Google **e** GitHub. A tentativa responde 409 "E-mail já vinculado a outro provedor social".

---

### SEC-05 — Segredo de assinatura de sessão guardado em texto claro no banco · **CONFIRMADO** · P1

**Arquivos:** [app/core/signing.py:38](app/core/signing.py:38), [migrations/0011_app_secrets.sql](migrations/0011_app_secrets.sql)

**Evidência.** Quando `JWT_SECRET_KEY` não está no ambiente, o servidor gera `secrets.token_urlsafe(48)` e grava em `app_secrets.value` como `TEXT`.

**Análise do trade-off.** O comentário da migration argumenta: *"quem alcança esta tabela já alcança as demais, então guardá-lo aqui não amplia a superfície."* Isso é verdade para **leitura de dados** e falso para **impersonação**. Quem lê essa linha pode assinar um token para qualquer `user_id` e agir como qualquer usuário — via API, indefinidamente, sem deixar rastro distinguível nos logs de auditoria. Os vetores não são idênticos: um dump de backup, um snapshot, o painel web do Supabase, uma service-role key vazada ou um `SELECT` com permissão só-leitura dão acesso ao segredo sem dar acesso a escrita. O segredo também não tem caminho de rotação: trocá-lo derruba todas as sessões de uma vez, sem período de sobreposição.

**Consequência.** Enquanto `JWT_SECRET_KEY` não estiver configurada no ambiente da Vercel, a produção está nesse modo. A auditoria anterior registrou que restou "uma única variável para você cadastrar" — enquanto ela não for cadastrada, este achado está ativo.

**Correção:** cadastrar `JWT_SECRET_KEY` no ambiente e rebaixar o fallback a um caminho de *desenvolvimento* explícito (logar `WARNING` em produção). Se o fallback precisar continuar existindo em produção, guardar o segredo cifrado por uma chave de ambiente já contraria o propósito — a solução honesta é a variável de ambiente.

---

### SEC-06 — Sem `TrustedHostMiddleware`; `Host` do atacante alimenta redirect de OAuth · **CONFIRMADO** · P1

**Arquivos:** [app/main.py:3148](app/main.py:3148), [app/oauth.py:52](app/oauth.py:52), [app/oauth.py:63](app/oauth.py:63)

**Evidência.** As três rotas de OAuth fazem `set_request_origin(str(request.base_url))`. `request.base_url` do Starlette é construída a partir do header `Host`. Nenhum `TrustedHostMiddleware` está montado e `ALLOWED_ORIGINS` está vazio em produção (mesma origem).

`oauth_frontend_callback_url()` cai nesse valor quando `OAUTH_FRONTEND_CALLBACK_URL` e `ALLOWED_ORIGINS` estão vazios — que é exatamente a configuração de produção descrita no `.env.example`.

**Comportamento.** Uma requisição com `Host: evil.example` faz `frontend_redirect()` emitir `302` para `https://evil.example/oauth/callback?session=1`.

**Consequência.** Open redirect a partir de um domínio confiável — útil para phishing. O `redirect_uri` forjado enviado ao provedor seria rejeitado (os provedores validam contra allowlist registrada), então **não** há roubo de token por esse caminho, e o cookie de sessão é emitido para o domínio real. Severidade **média**, mas a ausência de allowlist de host é uma lacuna de configuração barata de fechar.

---

# 6. Complete Findings

Agrupados por domínio. Severidade entre parênteses.

## 6.1 Banco de dados e schema

| ID | Achado | Evidência | Sev. |
|---|---|---|---|
| DB-01 | **`card_pins` não usa a FK composta** — `REFERENCES cards(id)` e `REFERENCES users(id)` separados. É a única tabela que rompe a invariante de isolamento elogiada na auditoria anterior; um bug poderia associar o PIN de um usuário ao cartão de outro. | [migrate.py:110](migrate.py:110) | Média |
| DB-02 | **Migration `0007` não existe.** Sequência: 0001–0006, 0008–0012. `git log --diff-filter=D -- migrations/` não mostra remoção. O `sorted(glob)` de `apply_versioned_migrations` não se importa, mas a lacuna sugere migration escrita e perdida. | `ls migrations/` | Média |
| DB-03 | **Duas fontes de verdade de schema.** `login_failures_state` definida em `SCHEMA_SQL` *e* em `0005`. `categories.is_active`/`updated_at` em `SCHEMA_SQL` *e* em `0006`/`0010`. | [migrate.py:167](migrate.py:167) vs `migrations/0005` | Média |
| DB-04 | **`transaction_date` e `billing_month` são `TEXT`.** Sem constraint de formato. Funciona porque ISO-8601 ordena lexicograficamente, e há índice de expressão. Custo: `substring()` em toda query, nenhuma aritmética de data em SQL, nenhuma garantia de que o banco só contenha datas válidas. | [migrate.py:93](migrate.py:93) | Média |
| DB-05 | **`transactions` não tem `updated_at`.** Só `created_at`. Num app financeiro, não é possível saber quando um lançamento foi editado nem ordenar por modificação. `users`, `categories` e `budgets` já têm coluna + trigger. | [migrate.py:82](migrate.py:82) | Média |
| DB-06 | **Três colunas de reserva em `settings`:** `reserve_amount`, `reserve_goal_amount`, `reserve_current_amount`. Semânticas distintas (planejado no mês / meta total / saldo atual) mas nomes que não as distinguem, e nada as relaciona. | [migrate.py:47](migrate.py:47), [app/main.py:1832](app/main.py:1832) | Baixa |
| DB-07 | **Nenhuma escrita multipasso é atômica entre `db_cursor` diferentes.** `db_cursor(commit=True)` abre conexão própria; duas chamadas sequenciais são duas transações. As rotas críticas (CSV confirm, installments, register) fazem tudo num bloco só — correto. Mas o padrão não é imposto e `delete_card`/`update_transaction` fazem leitura e escrita em blocos separados (race de lost-update). | [app/core/database.py:74](app/core/database.py:74) | Baixa |
| DB-08 | **`get_export_transactions` e outros não limitam resultado**; `list_transactions` tem `LIMIT 250` fixo sem paginação nem indicação ao cliente de que houve truncamento. | [app/main.py:1124](app/main.py:1124) | Média |

## 6.2 Domínio financeiro

| ID | Achado | Evidência | Sev. |
|---|---|---|---|
| FIN-01 | **Hash de duplicata não inclui hora nem conta.** `build_duplicate_hash(user_id, date, description, amount, type)`. Com o índice **único** `idx_transactions_user_duplicate_hash`, duas passagens de ônibus de R$ 6,00 no mesmo dia com a mesma descrição são impossíveis de importar juntas — a segunda é silenciosamente descartada como duplicata. Lançamentos manuais não são afetados (não recebem hash). | [app/integrations/normalizer.py:15](app/integrations/normalizer.py:15), [migrate.py:176](migrate.py:176) | **Alta** |
| FIN-02 | **O calendário diário de metas e o total do mês discordam.** `day_map` filtra `transaction_date BETWEEN start AND end AND (billing_month IS NULL OR billing_month = month)` — exclui parcelas compradas em meses anteriores. `outflow_to_today` filtra pelo mês efetivo — inclui essas parcelas. As barras do calendário não somam o total exibido. | [app/main.py:1875](app/main.py:1875) vs [app/main.py:1901](app/main.py:1901) | Média |
| FIN-03 | **Score de "reservas" casa nome de categoria por string.** `WHERE lower(c.name) IN ('reserva','investimentos')`. Renomear ou arquivar a categoria padrão zera silenciosamente esse eixo do Ritmo Score. Falta um papel semântico na categoria (`role`/`kind`). | [app/main.py:2334](app/main.py:2334) | Média |
| FIN-04 | **O domínio não modela transferência, estorno nem pagamento de fatura.** `type` só admite `income`/`expense`, `amount > 0`. Consequências: mover dinheiro entre contas aparece como despesa + receita (inflando ambos os lados e o score); um estorno de compra tem de ser lançado como receita, contaminando a renda; pagar a fatura do cartão, se lançado, conta duplo com as compras. | [migrate.py:85](migrate.py:85) | **Alta** (produto) |
| FIN-05 | **Não há data de competência.** O par (data da compra, mês de fatura) cobre cartão, mas não cobre uma despesa paga em setembro referente a agosto. `billing_month` é reaproveitado como "mês contábil", o que funciona por acidente e não por desenho. | Convenção `COALESCE(billing_month, substring(...))` | Média |
| FIN-06 | **Nenhuma noção de transação pendente vs consolidada.** Toda linha é definitiva. Extratos de cartão trazem lançamentos pendentes; Open Finance traria mais ainda. | schema `transactions` | Média |
| FIN-07 | **Moeda é decorativa.** `settings.currency` existe, default `'BRL'`, e nenhuma query ou formatação a consulta — `format_brl` é fixa. Campo que promete algo que não entrega. | [app/shared/money.py:35](app/shared/money.py:35) | Baixa |
| FIN-08 | **`first_billing_month` não trata `closing_day` maior que o número de dias do mês.** Cartão com fechamento no dia 31, compra em 28 de fevereiro: `28 > 31` é falso, então cai em fevereiro — correto por acaso. Compra em 30 de abril com fechamento 31: também cai em abril. O resultado é aceitável, mas a regra não está expressa nem testada. | [app/shared/dates.py:47](app/shared/dates.py:47) | Baixa |
| FIN-09 | **`update_transaction` permite editar linha de parcelamento sem restrição:** trocar `type` para `income`, mudar `billing_month`, mudar `amount`. O grupo fica internamente inconsistente e nada revalida. Também não recalcula `duplicate_hash`, deixando hash obsoleto em lançamentos importados editados. | [app/main.py:4113](app/main.py:4113) | Média |
| FIN-10 | **`delete_transaction` apaga o grupo inteiro de parcelas sem sinalização prévia.** A API não recebe flag de confirmação; só devolve `deletedGroup: true` depois do fato. | [app/main.py:5076](app/main.py:5076) | Média |
| FIN-11 | **Parcelamento limitado a 24x** (`totalInstallments: le=24`), enquanto 36x e 48x são comuns no varejo brasileiro. | [app/main.py:2722](app/main.py:2722) | Baixa |

## 6.3 CSV

| ID | Achado | Evidência | Sev. |
|---|---|---|---|
| CSV-01 | **Valores em formato internacional são lidos errado.** `parse_decimal_text` assume sempre que `.` é milhar e `,` é decimal quando os dois aparecem. `"1,234.56"` → remove o ponto → `"1,23456"` → `1.23456`. Vira **R$ 1,23** em vez de R$ 1.234,56. Vários bancos digitais e planilhas exportadas em locale en-US produzem esse formato. A regra correta é: o separador que aparece **por último** é o decimal. | [app/integrations/normalizer.py:29](app/integrations/normalizer.py:29) | **Alta** |
| CSV-02 | **Valor negativo entre parênteses não é reconhecido.** `"(123,45)"` → o regex remove os parênteses → `123,45` positivo. Se não houver coluna de tipo, `parse_import_type` classifica como receita. Despesa vira entrada. | [app/integrations/normalizer.py:28](app/integrations/normalizer.py:28) | Média |
| CSV-03 | **Sinal posposto quebra o parse.** `"1.234,56-"` (usado por alguns exports) vira `"1.234,56-"` e `Decimal()` levanta — a linha entra em `errors` sem explicação útil. | idem | Baixa |
| CSV-04 | **Cabeçalho tem que estar na linha 1.** `csv.DictReader` lê a primeira linha como header. Extratos de Itaú, Bradesco e Santander trazem linhas de preâmbulo (nome do banco, período, agência) antes do cabeçalho. Nesses arquivos a importação falha com "CSV sem cabeçalho" ou monta colunas absurdas. | [app/main.py:548](app/main.py:548) | **Alta** (produto) |
| CSV-05 | **Delimitador só detecta `;` e `,`.** Arquivos separados por tabulação ou pipe não são suportados. | [app/main.py:543](app/main.py:543) | Baixa |
| CSV-06 | **Fallback de encoding é `latin-1`, não `cp1252`.** `latin-1` nunca levanta, então um arquivo Windows-1252 decodifica sem erro mas com aspas curvas e travessões virando caracteres de controle nas descrições. | [app/main.py:550](app/main.py:550) | Baixa |
| CSV-07 | **`csv_safe_cell` corrompe o round-trip.** Exportar uma descrição que começa com `-` produz `'-...` no CSV; reimportar traz o apóstrofo junto. Proteção contra injeção de fórmula é correta, mas precisa ser revertida na leitura. | [app/main.py:572](app/main.py:572) | Baixa |
| CSV-08 | **`account` é gravado na coluna `payment_method`.** `payment_method = row.get("account") or "csv_import"`, truncado em 50 caracteres. Mistura dois conceitos (forma de pagamento vs conta de origem) numa coluna só, e polui o `paymentMethodBreakdown` do dashboard com nomes de conta. | [app/main.py:3627](app/main.py:3627) | Média |
| CSV-09 | **`external_id` recebe o `duplicate_hash`.** Dois campos com propósitos diferentes recebendo o mesmo valor; `external_id` fica inutilizável para Open Finance depois. | [app/main.py:3645](app/main.py:3645) | Baixa |
| CSV-10 | **`POST /api/imports/csv/upload` não tem rate limit** e grava até 1 MB de JSONB por chamada em `csv_import_sessions_state`. A limpeza (`cleanup_csv_import_sessions`) só roda como efeito colateral do *próximo* upload. | [app/main.py:3534](app/main.py:3534) | Média |
| CSV-11 | **`confirm` recalcula o preview inteiro**, então o que o usuário confirmou e o que é aplicado podem divergir se categorias ou regras mudarem no intervalo. Também dobra o custo (ver PERF-01). | [app/main.py:3589](app/main.py:3589) | Média |

## 6.4 Segurança (além dos críticos)

| ID | Achado | Evidência | Sev. |
|---|---|---|---|
| SEC-07 | **Sessão de 168 horas (7 dias) sem refresh nem timeout de inatividade.** `ACCESS_TOKEN_EXPIRE_HOURS` default 168. Revogação só por logout explícito ou troca de senha. Para app financeiro é longo. | [app/core/config.py:24](app/core/config.py:24) | Média |
| SEC-08 | **Exportação LGPD inclui `card_pins.pin_hash`.** `build_data_export` descobre tabelas por `user_id`, pula as que terminam em `_state` e as de `_SKIP_TABLES = {"revoked_tokens"}`. `card_pins` não casa nenhum dos dois filtros. O princípio declarado no próprio módulo — "never the password hash" — é violado para o PIN. | [app/privacy/service.py:22](app/privacy/service.py:22) | Média |
| SEC-09 | **Revogar consentimento `terms_privacy` é no-op.** Grava no ledger `granted=false` e a conta continua plenamente ativa. Do ponto de vista de LGPD, registra-se uma revogação que não produz efeito. | [app/main.py:3435](app/main.py:3435) | Média |
| SEC-10 | **`revoke_token` após `DELETE FROM users` falha e é engolida.** `revoked_tokens.user_id` tem FK cascade; o `INSERT` posterior levanta `ForeignKeyViolation`, capturada por `except Exception` e apenas logada. Não é explorável (o usuário some, `get_current_user` devolve 401), mas gera um traceback por exclusão de conta. | [app/main.py:3405](app/main.py:3405) | Baixa |
| SEC-11 | **Remoção do avatar na exclusão de conta é best-effort silencioso.** `storage.remove_avatar` engole exceções. Se o Supabase falhar, o objeto permanece no bucket e a eliminação LGPD fica incompleta sem registro. | [app/core/storage.py:96](app/core/storage.py:96) | Média |
| SEC-12 | **`login` devolve `access_token` no corpo JSON** além de setar o cookie. O SPA descarta, mas o token trafega e pode ser logado por proxies/DevTools sem necessidade. | [app/main.py:3221](app/main.py:3221) | Baixa |
| SEC-13 | **`bandit -ll`** só reporta severidade média para cima; achados LOW nunca aparecem. Não há SAST além disso (sem CodeQL, sem `semgrep`). | `.github/workflows/ci.yml` | Baixa |
| SEC-14 | **CSP exige `'unsafe-inline'` em `script-src`** por causa do bootstrap de hidratação do export estático do Next e do `ThemeScript` inline. Documentado no código; é uma limitação real do modo `output: "export"` (sem nonce). Registrado como aceito, não como defeito. | [app/main.py:409](app/main.py:409) | Informativo |
| SEC-15 | **`pip-audit` audita `requirements.txt`, não `requirements-lock.txt`.** O Docker/Railway instala o lock. O artefato efetivamente publicado como imagem não é auditado. | `.github/workflows/ci.yml` | Média |

## 6.5 Performance

| ID | Achado | Evidência | Sev. |
|---|---|---|---|
| PERF-02 | **N+1 em `get_cards_summary`.** Por cartão: 1 query de fatura + 1 de grupos + 1 por grupo de parcelas + `get_card_commitment` com **conexão própria** aninhada. Com 3 cartões e 20 grupos: ~70 queries. | [app/main.py:1152](app/main.py:1152) | **Alta** |
| PERF-03 | **`/api/bootstrap` executa `get_cards_summary` duas vezes** (direto e dentro de `get_dashboard`) e `calculate_score` duas vezes (mês atual e anterior), cada uma com seu próprio laço `get_invoice_total` por cartão. `request_cached` cobre apenas `goals` e `budget_summary`. | [app/main.py:3468](app/main.py:3468) | **Alta** |
| PERF-04 | **Em serverless, cada `db_cursor()` abre conexão TCP+TLS nova.** Há 79 usos de `db_cursor` em `main.py`; um `/api/bootstrap` abre dezenas por request. | [app/core/database.py:58](app/core/database.py:58) | **Alta** |
| PERF-05 | **`/relatorios` = 240 kB de First Load JS** (121 kB de página), porque importa `recharts` estaticamente. O padrão de `dynamic()` já existe e funciona em `SummaryHome.tsx`. Medido no `next build`. | [frontend/app/relatorios/page.tsx:5](frontend/app/relatorios/page.tsx:5) | Média |
| PERF-06 | **Fontes latin e latin-ext declaradas sem `unicodeRange`.** `localFont` gera duas `@font-face` com descritores idênticos, anulando o propósito do subsetting. Para pt-BR, `latin-ext` provavelmente é desnecessária por completo. | [frontend/app/layout.tsx:10](frontend/app/layout.tsx:10) | Baixa |
| PERF-07 | **`simulate_card_invoices` faz uma query por mês** no laço (até 24). Reusa a conexão, então é menos grave, mas é agregação trivialmente consolidável. | [app/main.py:1350](app/main.py:1350) | Baixa |
| PERF-08 | **`downloadFile` nunca chama `URL.revokeObjectURL`.** Cada exportação vaza o blob até o reload. | [frontend/app/relatorios/page.tsx:21](frontend/app/relatorios/page.tsx:21) | Baixa |

## 6.6 Frontend, UX e acessibilidade

| ID | Achado | Evidência | Sev. |
|---|---|---|---|
| UX-01 | **Nenhum error boundary.** Não existe `app/error.tsx`, `app/global-error.tsx` nem `app/not-found.tsx`. Um erro de render em qualquer página mostra a tela padrão do Next, sem identidade e sem caminho de recuperação. | `find frontend/app -name "error.tsx"` → vazio | **Alta** |
| UX-02 | **Padrão de diálogo inconsistente.** `ImportConfirmDialog` tem `role="dialog"`, `aria-modal`, focus trap, `Escape` e restauração de foco. `CreateCategoryDrawer` e `FinancialPlanningDrawer` não têm nenhum dos cinco — só um botão com `aria-label="Fechar"`. | `grep role=/aria-modal` nos três | Média |
| UX-03 | **Trocar a senha desloga silenciosamente.** `change_password` grava `password_changed_at`; `get_current_user` passa a rejeitar o token atual (`iat < password_changed_at`). Correto do ponto de vista de segurança, mas nenhum token novo é emitido e o usuário é jogado para `/login` sem explicação. | [app/main.py:3336](app/main.py:3336) | Média |
| UX-04 | **Sem manifest PWA nem `theme-color`**, num produto que o README descreve como "mobile first". Não instalável, sem cor de barra de status. | `frontend/app/layout.tsx`, `frontend/public/` | Média |
| UX-05 | **Cache GET de 30 s sem invalidação por navegação.** Além do SEC-01, uma alteração feita em outra aba não aparece por até 30 s. | [frontend/lib/api.ts:23](frontend/lib/api.ts:23) | Baixa |
| UX-06 | **`console.error` em `.catch()` de handlers de UI** (`importar/page.tsx`) — erro engolido no console em vez de virar estado de erro visível. | [frontend/app/importar/page.tsx:382](frontend/app/importar/page.tsx:382) | Baixa |
| UX-07 | **O modo "substituir" da importação não explica o que apaga.** O diálogo mostra `existingInMonths`, mas a linguagem sugere substituir importações, não apagar lançamentos manuais (ver DATA-01). | `ImportConfirmDialog.tsx` | **Alta** (acoplado a DATA-01) |
| UX-08 | **Labels são implícitos (input dentro de `<label>`)** — válido em WCAG. Registrado aqui apenas para constar que foi verificado e **não** é um defeito. | `ExpenseForm.tsx:60` | Informativo |

## 6.7 Testes

| ID | Achado | Evidência | Sev. |
|---|---|---|---|
| TEST-01 | **Nenhum teste de integração exercita o caminho real de autenticação.** `auth_headers` emite `Authorization: Bearer`, que é explicitamente **isento** do middleware CSRF. Produção usa cookie + `X-CSRF-Token`, e esse caminho nunca é testado ponta a ponta. | [tests/conftest.py:115](tests/conftest.py:115), [app/main.py:357](app/main.py:357) | **Alta** |
| TEST-02 | **Nenhum teste E2E.** Sem Playwright/Cypress. Fluxos de várias etapas (importação CSV em 4 passos, callback OAuth, exclusão de conta) só existem testados por partes. | `tests/` | Média |
| TEST-03 | **Frontend tem 3 testes, só de `money.ts`**, e `npm run test:money` **não roda no CI**. | `package.json`, `ci.yml` | Média |
| TEST-04 | **Nenhuma fixture de extrato bancário real.** O `.gitignore` ignora `*.csv`, e o `check-ignored-files.sh` não verifica `.csv` — uma fixture seria engolida em silêncio, repetindo a classe de incidente que o script existe para prevenir. | `.gitignore:47`, `scripts/check-ignored-files.sh:28` | **Alta** |
| TEST-05 | **Nenhum teste de fuso horário, de `first_billing_month` para compra avulsa, de colisão de `installment_group`, nem de "substituir" preservando lançamentos manuais.** Ou seja: nenhum dos P0/P1 desta auditoria tem rede de proteção. | `tests/` | **Alta** |
| TEST-06 | **Gate de cobertura em 60 %** — baixo para app financeiro, mas o número importa menos que TEST-01/05: o risco não coberto é o que pesa. | `ci.yml` | Baixa |

## 6.8 CI/CD, dependências e operação

| ID | Achado | Evidência | Sev. |
|---|---|---|---|
| CI-01 | **A suíte roda 2 a 3 vezes por build.** O passo "Falhar se algum teste foi pulado" reexecuta `pytest` e faz `grep` na saída; em caso de falha, executa uma terceira vez para listar. | `ci.yml` | Média |
| CI-02 | **`build-and-push` publica imagem Docker no GHCR a cada push em `main`** — e nada consome essa imagem, já que o deploy é Vercel. Tempo de build e armazenamento gastos sem destinatário. | `ci.yml` | Média |
| CI-03 | **Nenhum passo de migration no deploy.** `vercel.json` não tem `buildCommand` que execute `migrate.py`. A causa raiz apontada pela auditoria anterior segue aberta; o contorno em runtime traz OPS-01. | `vercel.json` | **Alta** |
| CI-04 | **Sem `concurrency` group** — pushes em sequência disparam builds paralelos redundantes. | `ci.yml` | Baixa |
| CI-05 | **Sem `npm audit` no frontend**, embora o Dependabot esteja configurado para npm. | `ci.yml` | Baixa |
| CI-06 | **Nenhum error tracking, APM ou alerta.** Existem logs estruturados (`JsonLogFormatter`) e `X-Request-Id`, mas nada os coleta. Um 500 em produção só é descoberto por reclamação ou inspeção manual de logs da Vercel. | `app/core/logging.py` | **Alta** |
| CI-07 | **Nenhuma política de backup documentada nem testada.** `docs/security/` tem checklist e resposta a incidente, mas não há procedimento de restore verificado. Para 27 MB de dados financeiros de terceiros, é a lacuna operacional mais séria. | `docs/` | **Alta** |
| DEP-01 | **Dependências de teste em `requirements.txt`:** `pytest`, `pytest-asyncio`, `pytest-cov`, `factory-boy`, `freezegun`, `bandit`, `ruff`. A Vercel instala desse arquivo — tudo isso vai para o bundle serverless, aumentando cold start e superfície. | `requirements.txt:11-18` | **Alta** |
| DEP-02 | **`lucide-react` (^0.468.0) não é importado em lugar nenhum.** Zero ocorrências em `app/`, `components/`, `lib/`. | `grep -rn lucide-react` → vazio | Média |
| DEP-03 | **`freezegun` não é usado** por nenhum teste. | `grep -rn freezegun` → só `requirements*` | Baixa |
| DEP-04 | **`python-json-logger` não é importado.** `app/core/logging.py` implementa o formatter à mão. | `grep -rn pythonjsonlogger` → vazio | Baixa |
| DEP-05 | **`supabase==2.30.1`** (+ `supabase-auth`, `supabase-functions`, `supabase-storage`, `postgrest`, `realtime`, `gotrue`, `websockets`, `httpx[http2]`) instalada para **três chamadas HTTP**: upload, `create_signed_url`, `remove`. `httpx` já é dependência direta. | `app/core/storage.py`, `requirements-lock.txt` | Média |
| DEP-06 | **`passlib==1.7.4` (último release em 2020) obriga `bcrypt<4.1`.** O pin existe porque passlib 1.7.4 quebra com bcrypt ≥ 4.1. Resultado: a biblioteca de hash de senha de um app financeiro está congelada em out/2023 para acomodar um wrapper abandonado. | `requirements.txt:6-7`, `requirements-lock.txt` | **Alta** (manutenção) |
| DEP-07 | **`ecdsa==0.19.2` com PYSEC-2026-1325 sem correção publicada**, ignorada no CI com justificativa correta (tokens são HS256). A saída limpa é trocar `python-jose` por `PyJWT`, que não traz `ecdsa` nem `rsa`. | `ci.yml` | Média |
| DEP-08 | **`pydantic` sem upper bound** (vem transitivo do FastAPI) e todos os modelos usam `class Config` do Pydantic v1, deprecado no v2. Um Pydantic v3 futuro remove o suporte e `extra = "forbid"` para de valer — silenciosamente, em todos os 22 payloads. | `requirements.txt:1`, `app/main.py:2644` | Média |
| CFG-01 | **`render.yaml` está quebrado e com a marca antiga** (`ritmo-financeiro-pro`): não constrói o frontend, então o alvo Render serve só API. | `render.yaml` | Média |
| CFG-02 | **`docker-compose.yml` usa `ritmo` como usuário/banco** e o CI usa `ritmo`/`ritmo_test`. Resíduo da marca anterior à anterior. | `docker-compose.yml`, `ci.yml` | Baixa |
| CFG-03 | **`CategoryPayload.color` tem default `#9be768`** — o verde da marca antiga. Um cliente que omita `color` cria categoria fora da paleta. | [app/main.py:2679](app/main.py:2679) | Baixa |
| CFG-04 | **`.gitignore` ignora `*.csv` e `*.pdf`**, os dois formatos centrais do produto, e `check-ignored-files.sh` não cobre essas extensões. Mesma classe do incidente `secrets.py`, ainda armada. | `.gitignore:47-48` | **Alta** |
| CFG-05 | **`Makefile lint` roda `ruff check app/ tests/`**, enquanto o CI roda `ruff check .` — o comando local não cobre `migrate.py`, `api/`, `main.py`. | `Makefile` | Baixa |
| CFG-06 | **Versão `2.0.0` está escrita em três lugares** (`FastAPI(version=...)` e duas vezes no handler de `/api/health`), e o `CHANGELOG.md` não tem nenhuma versão publicada. | [app/main.py:255](app/main.py:255) | Baixa |
| DEAD-01 | **Código morto:** `app/core/errors.py` (helpers `bad_request`/`not_found`, zero usos), `app/core/middleware.py` (só docstring), `app/integrations/base.py` (`ImportedTransaction`/`FinancialDataSource`, zero importações). | `grep` | Baixa |
| DOC-01 | **`app/integrations/open_finance/README.md` descreve um `TransactionNormalizer` que não existe** no código. | idem | Baixa |

---

# 7. Unnecessary Complexity

O que existe, custa manutenção e não paga o custo.

### 7.1 O gerador de PDF escrito à mão — ≈420 linhas

[app/main.py:4291-4712](app/main.py:4291): `PdfReport`, `pdf_escape`, `pdf_color`, `pdf_color_command`, `wrap_pdf_line`, `build_basic_pdf`, `add_pdf_section`, `add_pdf_summary_cards`, `add_pdf_bar_rows`, `add_pdf_table`, `build_report_pdf`.

O código monta o arquivo PDF byte a byte, com objetos, xref e operadores de desenho. Consequências que já estão no código: `pdf_escape` faz `.encode("latin-1", "replace")` — ou seja, **qualquer caractere fora de latin-1 vira `?` no relatório**, e o produto é em português com nomes de categoria livres (emoji nos ícones, por exemplo). Não há quebra de página inteligente além de `wrap_pdf_line(max_length=92)` por contagem de caracteres, o que quebra com fonte proporcional.

**Alternativa mais simples:** `reportlab` ou `fpdf2` — uma dependência, suporte a UTF-8, tabelas e paginação de verdade, ~60 linhas de código de layout. Ou, ainda mais simples: eliminar o PDF e manter só o CSV, já que o CSV é o formato que o usuário realmente reaproveita. **Recomendação:** trocar por `fpdf2` no BP-10, ou remover se a telemetria mostrar uso baixo.

**Por que a complexidade extra NÃO se justifica:** a única vantagem de escrever à mão é evitar uma dependência. Trocar 420 linhas de código proprietário que corrompe acentos por ~40 linhas sobre uma biblioteca madura é redução de risco, não aumento.

### 7.2 A biblioteca `supabase` inteira para três chamadas HTTP

`app/core/storage.py` usa exatamente: `.storage.from_(bucket).upload()`, `.create_signed_url()`, `.remove()`. Por isso entram no bundle: `supabase`, `supabase-auth`, `supabase-functions`, `storage3`/`supabase-storage`, `postgrest`, `realtime`, `gotrue`, `websockets`, `httpx[http2]`.

**Alternativa mais simples:** três chamadas `httpx` à Storage REST API (`POST /storage/v1/object/{bucket}/{path}`, `POST /storage/v1/object/sign/...`, `DELETE ...`) com o header `Authorization: Bearer <service_role_key>`. `httpx` já é dependência direta. Reduz o bundle serverless materialmente e elimina uma família inteira de atualizações do Dependabot.

### 7.3 Três alvos de deploy para um produto com um deploy

`vercel.json` + `Dockerfile`/`docker-compose.yml` + `railway.json` + `render.yaml`. O Render está quebrado. O Railway não é usado. O Docker existe para o dev local, o que é legítimo — mas o `build-and-push` do CI publica uma imagem em GHCR que nada consome.

**Recomendação:** manter Vercel (produção) e Docker (dev local + fallback deliberado). Remover `render.yaml`. Remover o job `build-and-push` ou torná-lo condicional a uma tag de release.

### 7.4 `app/core/errors.py`, `app/core/middleware.py`, `app/integrations/base.py`

Três arquivos sem um único importador. `errors.py` é uma abstração criada antes de ser necessária (`HTTPException` direto é usado em 100 % do código). `middleware.py` tem só uma docstring dizendo que o middleware está em outro lugar. `base.py` define um `Protocol` para uma integração que não existe.

**Recomendação:** remover `errors.py` e `middleware.py`. **Manter** `base.py` — ele é o contrato de desenho do Open Finance e tem valor documental, mas deve sair de `app/integrations/` e virar parte do documento de decisão (ou receber o comentário explícito de que é especificação, não código vivo).

### 7.5 O modo "substituir" da importação

Além de ser destrutivo (DATA-01), é uma feature cuja utilidade não é proporcional ao risco. O caso de uso real — "importei errado, quero refazer" — é melhor servido por "desfazer a última importação", que é trivialmente seguro (apaga por lote de importação) e não exige que o usuário entenda a semântica de "meses presentes no arquivo".

**Recomendação:** substituir "replace" por "desfazer importação". Isso exige uma coluna `import_batch_id` em `transactions` — uma coluna, contra um modo de operação que pode destruir histórico.

### 7.6 O que NÃO é complexidade desnecessária

Registro explícito para evitar que uma futura limpeza destrua algo valioso:

- **`PlainDictRoute`** parece um hack, mas resolve um problema real de serialização e está documentado com o porquê. Manter.
- **`request_cached` com `ContextVar`** é a solução mais simples possível para o problema (deduplicar agregações dentro de uma request) e não tem risco de servir dado velho. Manter e **estender**.
- **As FKs compostas** parecem redundantes com `user_id` já na tabela. Não são: são a garantia de isolamento. Manter e **completar** (`card_pins`).
- **`ensure_serverless_schema`** resolve um problema real. O que está errado é *o que ele executa* (OPS-01), não o fato de existir.
- **O `check-ignored-files.sh`** é uma correção estrutural exemplar. Manter e **ampliar** a lista de extensões.

---

# 8. Missing Capabilities

O que deveria existir e não existe. Separado entre *requisito real*, *melhoria desejável* e *fora de escopo agora*.

## Requisito real (a ausência causa dano hoje)

| Capacidade | Por que é requisito | Achados relacionados |
|---|---|---|
| **Error tracking em produção** | Um 500 hoje só é descoberto por reclamação. O `JsonLogFormatter` e o `X-Request-Id` já existem — falta um coletor. Sentry free cobre o volume atual. | CI-06 |
| **Backup verificado e restore testado** | 13 usuários reais, 27 MB de dados financeiros. Existe documentação de segurança; não existe procedimento de restore testado. | CI-07 |
| **Error boundaries no frontend** | Nenhuma página tem recuperação de erro de render. | UX-01 |
| **Teste do caminho de autenticação real** | Cookie + CSRF é o que produção usa e é o que nenhum teste toca. | TEST-01 |
| **Fixtures de extratos bancários reais** | Sem elas, nenhuma correção de CSV pode ser validada — e o `.gitignore` impede que sejam versionadas. | TEST-04, CFG-04 |
| **Rate limiting que funcione** | Ver SEC-02. | SEC-02 |
| **Fuso horário do usuário** | Ver DOM-04. | DOM-04 |
| **`import_batch_id` em `transactions`** | Habilita "desfazer importação" e torna DATA-01 corrigível de verdade. | DATA-01, 7.5 |

## Melhoria desejável (agrega valor claro, sem urgência)

| Capacidade | Racional |
|---|---|
| **Transferência entre contas como tipo de transação** | Resolve FIN-04 e elimina inflação artificial de receita e despesa. |
| **Estorno / lançamento negativo** | Hoje impossível (`CHECK amount > 0`); modelável como um `type` novo, sem mudar a coluna. |
| **Papel semântico na categoria (`role`: reserva, investimento, moradia…)** | Resolve FIN-03 e destrava recomendações melhores sem string matching. |
| **Paginação em `/api/transactions`** | O `LIMIT 250` fixo é invisível ao cliente. |
| **Manifest PWA + `theme-color`** | Produto mobile-first não instalável. |
| **Vincular múltiplos provedores sociais** | Hoje um par `(provider, subject)` por usuário. |
| **Parcelamento acima de 24x** | 36x/48x são comuns no varejo brasileiro. |
| **Cabeçalho de CSV fora da linha 1** | Destrava Itaú, Bradesco, Santander. |
| **`updated_at` em `transactions`** | Rastreabilidade de edição num app financeiro. |
| **E2E de um fluxo crítico** | Importação CSV completa, em Playwright. |

## Fora de escopo agora (justificativa na seção 19)

Multi-moeda real, contas bancárias como entidade, orçamento envelope, metas de longo prazo, notificações push, relatórios anuais, exportação OFX, Open Finance direto.

---

# 9. Security Review

## Sumário por severidade

| Severidade | Achados |
|---|---|
| **CRITICAL** | Nenhum achado atinge CRITICAL isoladamente. SEC-01 e SEC-02 combinados (vazamento entre usuários + ausência de limite de tentativas) chegam perto num cenário de dispositivo compartilhado. |
| **HIGH** | SEC-01 (cache vaza dados entre usuários) · SEC-02 (rate limit inoperante) · SEC-04 (vinculação OAuth sem confirmação) · SEC-05 (segredo de assinatura em claro no banco) · DEP-01 (deps de teste em produção) · DEP-06 (`passlib` abandonado prendendo `bcrypt`) |
| **MEDIUM** | SEC-06 (sem allowlist de host; open redirect) · SEC-07 (sessão de 7 dias sem refresh) · SEC-08 (`pin_hash` na exportação LGPD) · SEC-09 (revogação de consentimento inócua) · SEC-11 (eliminação de avatar best-effort) · SEC-15 (lock não auditado) · DEP-07 (`ecdsa` vulnerável via `python-jose`) · DEP-08 (`pydantic` sem teto + `class Config` v1) |
| **LOW** | SEC-10 (traceback na exclusão de conta) · SEC-12 (token no corpo da resposta) · SEC-13 (`bandit -ll` esconde LOW) · CFG-04 (`.gitignore` como armadilha) |
| **Aceito / informativo** | SEC-14 (`'unsafe-inline'` obrigatório no export estático do Next) |

## O que está bem feito e deve ser preservado

- Cookie `HttpOnly` + `SameSite=Lax` + `Secure` em produção; token nunca no `localStorage`.
- CSRF double-submit exigido só quando a autenticação vem por cookie — decisão correta, bem comentada.
- Revogação de token por hash, persistida, com `expires_at`.
- Invalidação por `iat < password_changed_at`, com `date_trunc('second')` para evitar falso positivo no mesmo segundo. Cuidado raro.
- bcrypt contra hash fictício quando o e-mail não existe (defesa contra enumeração por tempo).
- Rate limit de login por e-mail **persistido em Postgres** — esta camada funciona.
- Cabeçalhos completos: CSP, HSTS, `nosniff`, `frame-ancestors 'none'`, `Permissions-Policy`, COOP/CORP, `Cache-Control: no-store` em `/api/`.
- Logs de auditoria com e-mail e IP hasheados.
- PIN de cartão com bcrypt, sessão de desbloqueio de 15 min, bloqueio por tentativas persistido, tudo com FK composta.
- Validação de assinatura de arquivo (magic bytes) no upload de avatar, além do `Content-Type`.
- Proteção contra injeção de fórmula no CSV exportado.
- Path traversal tratado no servidor de estáticos (`..` em `Path.parts`, checagem de `is_absolute`).
- Zero SQL construída por concatenação de dados do usuário — todas as queries são parametrizadas. As duas exceções (`build_data_export`) usam identificadores validados por regex e vêm de `information_schema`, com `# nosec` justificado.

## Ordem de correção recomendada

1. SEC-01 (uma função exportada e duas chamadas — 30 minutos, impacto alto)
2. SEC-02 (mover contadores para Postgres, reaproveitando o padrão de `login_failures_state`)
3. SEC-05 (cadastrar `JWT_SECRET_KEY` no ambiente — ação operacional, não de código)
4. SEC-04 + SEC-06 (superfície de OAuth)
5. SEC-08 + SEC-09 + SEC-11 (conformidade LGPD)
6. DEP-01, DEP-06, DEP-07 (cadeia de dependências)

---

# 10. Data & Financial Integrity Review

## Onde o dinheiro pode ficar errado

| Risco | Mecanismo | Sev. |
|---|---|---|
| **Saldo e meta diária inflados** | `monthly_income` somado a `inflow` sem deduplicar salário (DOM-05) | **Alta** |
| **Fatura de cartão no mês errado** | Compra avulsa no crédito ignora `closing_day` (DOM-03) | **Alta** |
| **Juros divergentes ou ausentes** | Taxa arredondada para centavos (DOM-02) | **Alta** |
| **Perda de lançamentos manuais** | Importação "substituir" (DATA-01) | **Alta** |
| **Grupo de parcelas corrompido** | Colisão de `installment_group` (DOM-01) + exclusão parcial por DATA-01 | **Alta** |
| **Valores 1000× menores** | `parse_decimal_text` com formato `1,234.56` (CSV-01) | **Alta** |
| **Despesa importada como receita** | Negativo entre parênteses (CSV-02) | Média |
| **Lançamento legítimo descartado como duplicata** | Hash sem hora/conta + índice único (FIN-01) | **Alta** |
| **Números do calendário não batem com o total** | Filtros diferentes para série diária e total (FIN-02) | Média |
| **Histórico reclassificado em massa** | `ON CONFLICT DO UPDATE SET type` em categorias (SEC-03) | **Alta** |
| **Ritmo Score zerado sem motivo** | Eixo de reservas depende de nome literal de categoria (FIN-03) | Média |
| **"Hoje" e "mês atual" errados 3h por dia** | Cálculo em UTC (DOM-04) | **Alta** |

## O que está certo e não deve ser mexido

- `Decimal` com `ROUND_HALF_UP` em toda a cadeia; nenhum `float` em valor monetário persistido.
- `distribute_installments` opera em centavos inteiros e fecha a diferença na última parcela — soma exata garantida. Tem teste (`tests/unit/test_finance_service.py`).
- `NUMERIC(14,2)` em todas as colunas de dinheiro, com `CHECK (amount > 0)` e `CHECK (planned_amount >= 0)`.
- Sinal carregado pelo `type`, não pelo valor. Elimina a ambiguidade clássica de "-100 é despesa ou estorno de receita?".
- Índice único parcial em `(user_id, duplicate_hash)` garante idempotência da importação no nível do banco — a implementação tem o problema de FIN-01, mas o *mecanismo* é o correto.
- `confirm_csv_import` faz delete + todos os inserts numa única transação. Correto.
- `ON DELETE CASCADE` em `user_id` em todas as tabelas — a eliminação LGPD é atômica.

## Convenção de mês contábil

`COALESCE(billing_month, substring(transaction_date from 1 for 7))` aparece em ~15 queries. É simples e eficaz, e tem índice de expressão dedicado (0012). Duas ressalvas:

1. A expressão é repetida literalmente. Qualquer divergência de escrita (espaçamento, ordem) faz o planejador ignorar o índice. **Recomendação:** quando os roteadores forem extraídos (BP-09), promover a expressão a uma coluna gerada (`GENERATED ALWAYS AS ... STORED`) — com índice comum — ou a uma constante Python única.
2. Ela mistura dois conceitos: "fatura em que cai" (cartão) e "mês contábil" (tudo o mais). Funciona hoje; vai atrapalhar quando entrar data de competência (FIN-05).

---

# 11. UX / Accessibility / Performance Review

## Bundle medido (`next build`, 15/09/2026)

```
Route (app)                    Size    First Load JS
/                              361 B      103 kB
/cadastro                      1.8 kB     124 kB
/configuracoes                4.08 kB     120 kB
/dashboard                    10.5 kB     130 kB   ← recharts sob demanda: OK
/importar                     8.64 kB     128 kB
/login                        1.47 kB     124 kB
/metas                        5.55 kB     125 kB
/orcamento                    3.44 kB     127 kB
/parcelas                     2.93 kB     127 kB
/perfil                       5.54 kB     125 kB
/relatorios                    121 kB     240 kB   ← recharts estático
/transacoes                   6.08 kB     130 kB
+ shared                                   102 kB
```

`/relatorios` é o dobro de qualquer outra página, pelo mesmo motivo que o dashboard tinha antes da correção. O padrão para resolver já existe e está testado no `SummaryHome.tsx`.

**Lighthouse continua sem medição nova.** Não vou afirmar um LCP que não medi — a auditoria anterior deixou isso em aberto e continua em aberto. O que é mensurável e está medido é o bundle.

## Acessibilidade

**Verificado e correto:**
- Skip link em `AppShell` (`Pular para o conteúdo` → `#app-main`).
- `aria-current` em `BottomNav` e `SidebarItem`; `aria-current="step"` no fluxo de importação.
- `Select` custom: `role="listbox"`, `role="option"`, `aria-selected`, `aria-haspopup`, `aria-expanded`, `aria-controls`, `aria-describedby`, IDs via `useId`.
- `ImportConfirmDialog`: focus trap completo, `Escape`, `aria-modal`, `aria-labelledby`/`aria-describedby`, restauração de foco.
- Skeletons com `aria-busy`.
- Labels implícitos (input dentro de `<label>`) — padrão válido.
- Contrastes calculados e documentados no CSS, nos dois temas.
- Sem FOUC de tema (`ThemeScript` antes da hidratação).

**Gaps reais:**
- UX-01: nenhum error boundary.
- UX-02: `CreateCategoryDrawer` e `FinancialPlanningDrawer` sem `role="dialog"`, `aria-modal`, focus trap ou `Escape` — inconsistente com o padrão que o próprio projeto já estabeleceu.
- O handle de redimensionamento da sidebar responde a teclado (`ArrowLeft`/`ArrowRight`) mas não se anuncia: falta `role="separator"` com `aria-orientation`, `aria-valuenow`/`min`/`max`.
- Não há `aria-live` nas regiões de mensagem de erro/sucesso das páginas (o `FeedbackMessage` tem um atributo aria, mas as mensagens de `setMessage` inline não usam região viva).

## UX de produto

- **Fluxo de importação em 4 passos com preview, mapeamento e deduplicação** é a melhor parte do produto. Bem acima do que a maioria dos apps dessa categoria entrega.
- **Estados de erro/carregamento/vazio existem** (`EmptyState`, `Skeleton`, `FeedbackMessage`) e são usados. Bom.
- **Troca de senha desloga sem explicação** (UX-03).
- **Modo "substituir" com linguagem enganosa** (UX-07).
- **Sem PWA** num produto mobile-first (UX-04).
- **Exclusão de grupo de parcelas sem aviso prévio** (FIN-10).

---

# 12. Benchmarking

Comparação com produtos de finanças pessoais de porte semelhante — apps indie/pequenos times: **Actual Budget**, **Firefly III**, **Maybe Finance**, **Organizze** (BR), **Mobills** (BR). Explicitamente **não** comparo com Nubank, Itaú ou Mint: escala e time incompatíveis.

| Dimensão | Trevo | Comparáveis | Leitura |
|---|---|---|---|
| **Arquitetura** | Monólito modular, um arquivo de 5.133 linhas | Firefly III: Laravel com domínios separados. Actual: monorepo TS com pacotes. Maybe: Rails com `app/models` por domínio | O monólito modular é a escolha **certa** para o porte. O problema não é ser monólito — é não ser modular. Os comparáveis todos separam por domínio dentro do monólito. |
| **Isolamento multiusuário** | FK composta no banco | Firefly III: escopo por `user_id` na camada de aplicação (Eloquent global scope). Actual: um arquivo por orçamento | O Trevo está **acima** dos comparáveis aqui. Garantia no banco é mais forte que escopo de ORM. |
| **Modelagem de dinheiro** | `Decimal`/`NUMERIC(14,2)`, centavos inteiros no rateio | Actual: inteiros em centavos. Firefly: `decimal`. Maybe: `Money` gem | Em paridade com os melhores. |
| **Modelagem de domínio** | Sem transferência, estorno, conta bancária ou competência | Todos os comparáveis modelam **conta** e **transferência** como primitivas. Firefly III trata transferência como tipo de transação de primeira classe | **Abaixo do padrão da categoria.** Este é o maior gap de produto do Trevo, não o Open Finance. |
| **Importação** | CSV com mapeamento, preview, dedup por hash, regras de categorização | Firefly III: importador com mapeamento salvo por banco. Actual: importador OFX/QFX/CSV. Organizze/Mobills: OFX + Open Finance via agregador | O mecanismo do Trevo está bem feito; faltam **perfis de banco salvos** e tolerância a cabeçalho fora da linha 1 (CSV-04), que é exatamente o que os comparáveis resolvem. |
| **Categorização** | Regras por substring, avaliadas mais-longo-primeiro | Firefly III: regras com condições compostas e ações. Actual: regras + payee matching | Simples e suficiente para o estágio. Não recomendo ML nem nada mais elaborado agora. |
| **Autenticação** | JWT em cookie HttpOnly, CSRF, OAuth de 3 provedores, revogação, rate limit por e-mail | Firefly III: sessão Laravel + 2FA. Maybe: Devise + 2FA. Actual: senha do arquivo | **Acima da média**, com uma exceção: **nenhum comparável sério fica sem 2FA/TOTP**. Para um app financeiro, MFA é a lacuna de autenticação mais visível. |
| **Testes** | 80 unit + 28 integração (pulados sem DB); zero E2E; caminho cookie/CSRF não testado | Firefly III: PHPUnit extenso + testes de API. Actual: Jest + Playwright. Maybe: RSpec + system tests | **Abaixo.** O gap não é percentual de cobertura, é *qual risco está coberto*. |
| **Observabilidade** | Logs estruturados, `X-Request-Id`, health separado em liveness/readiness — e **nada coleta** | Sentry é quase universal nessa faixa | **Abaixo.** A instrumentação está pronta; falta o destino. |
| **Deploy** | Vercel serverless + Docker | Firefly III e Actual: Docker é o caminho padrão. Maybe: Render/Docker | Serverless com Postgres é uma escolha defensável para o volume atual, mas é a origem de vários achados (conexão por query, rate limit em memória, DDL no cold start). |
| **UX / Design** | Design system com contraste auditado, dark mode, mobile-first | Firefly III: funcional, feio. Actual: bom. Maybe: muito bom | **Acima da média** da categoria open source. |
| **Acessibilidade** | Skip link, focus trap, ARIA em componentes custom, contraste calculado | Raramente tratada nos comparáveis | **Acima.** Vale preservar isso como diferencial. |
| **Open Finance** | Placeholder | Organizze e Mobills usam agregador (Pluggy/Belvo). Firefly/Actual não têm (público internacional) | A conclusão da auditoria anterior está certa: agregador licenciado é o único caminho viável. Não é prioridade. |

## O que o benchmarking diz sobre prioridades

1. **O gap mais caro do Trevo não é Open Finance — é o modelo de domínio** (transferência, conta, estorno). Todo comparável resolve isso; o Trevo não, e isso produz números errados hoje.
2. **MFA é a única lacuna de autenticação em que o Trevo fica atrás.** Tudo o mais está em paridade ou acima.
3. **Perfis de importação por banco** (mapeamento salvo, tolerância a preâmbulo) é o que separa um importador demonstrável de um utilizável.
4. **Nada no benchmarking justifica** microserviços, fila, event bus, CQRS ou cache distribuído. Nenhum comparável de porte semelhante usa nada disso, e vários operam com volume maior.

---

# 13. Target Architecture

## Princípio

**Monólito modular, um processo, um banco.** A arquitetura-alvo não muda o formato do sistema — organiza o que já existe. Nenhum serviço novo, nenhuma fila, nenhum cache externo.

Justificativa contra alternativas maiores, item a item:

| Proposta | Problema concreto hoje? | Alternativa mais simples | Veredito |
|---|---|---|---|
| Microserviços | Não. Um único domínio, um único usuário por vez, 13 usuários. | Roteadores por domínio no mesmo processo. | **Não fazer.** |
| Fila / worker | Só a importação de CSV é longa — e ela fica curta ao corrigir PERF-01. | Corrigir o N+1. | **Não fazer agora.** Reavaliar se surgir sincronização Open Finance. |
| Cache distribuído (Redis) | Nenhum gargalo de leitura repetida entre requests. `request_cached` resolve o caso real. | Estender `request_cached`. | **Não fazer.** |
| Event bus / CQRS / event sourcing | Nenhuma necessidade de projeções múltiplas nem de auditoria temporal além de `updated_at`. | Colunas de timestamp. | **Não fazer.** |
| Kubernetes | Vercel e Docker cobrem produção e dev. | — | **Não fazer.** |
| ORM (SQLAlchemy) | SQL cru funciona e é legível; a migração custaria semanas e apagaria as FKs compostas do modelo mental. | Manter SQL cru, extraído para módulos de repositório. | **Não fazer.** |
| Trocar Vercel por container | Resolveria PERF-04, SEC-02 e OPS-01 de uma vez. Mas custa operação e dinheiro. | Corrigir os três individualmente. | **Reavaliar** se a carga crescer; não agora. |

## Estrutura-alvo do backend

```
app/
├── core/            config · database · security · signing · storage · logging
│                    (remover errors.py e middleware.py)
├── shared/          money · dates · clock  ← NOVO: fonte única de "agora" com fuso
├── api/
│   ├── deps.py      get_current_user, get_db, guardas comuns
│   └── middleware.py  request_id · content-type · csrf · security headers
├── auth/            router.py · service.py · schemas.py
├── transactions/    router.py · service.py · repository.py · schemas.py
├── cards/           router.py · service.py · repository.py · schemas.py
├── budgets/         …
├── categories/      …
├── goals/           …
├── dashboard/       …   (leitura; compõe os demais)
├── reports/         router.py · service.py · pdf.py  ← isolar o gerador aqui
├── imports/         router.py · service.py · parsers/  ← perfis de banco
├── privacy/         service.py (já existe) + router.py
└── main.py          ~150 linhas: app, middlewares, include_router, lifespan
```

**Regras de dependência:**
- `core` e `shared` não importam de domínio nenhum.
- Domínios importam de `core`/`shared` livremente.
- Domínios **não importam uns dos outros**, exceto `dashboard` e `reports`, que são composição de leitura e podem importar os `service` dos demais.
- `main.py` importa só routers.

## Estrutura-alvo do frontend

A estrutura atual (`app/` por rota, `components/` plano, `lib/`) é adequada ao tamanho. Duas mudanças:

- Adicionar `app/error.tsx`, `app/global-error.tsx`, `app/not-found.tsx`.
- Agrupar componentes por domínio quando `components/` passar de ~40 arquivos (está em 38). Não antes.

## Estrutura-alvo do schema

- `migrations/0000_baseline.sql` congelado a partir do `SCHEMA_SQL` atual; `SCHEMA_SQL` some do `migrate.py`.
- `ensure_serverless_schema` passa a só **verificar** (`SELECT count(*) FROM schema_migrations`) e alertar, nunca aplicar.
- Aplicação de migrations vira passo explícito de deploy.
- Novas colunas: `transactions.updated_at`, `transactions.import_batch_id`, `categories.role`.
- `card_pins` ganha FK composta `(user_id, card_id) REFERENCES cards(user_id, id)`.
- **`transaction_date`/`billing_month` continuam `TEXT` por enquanto** — a migração para `DATE` é cara (toca ~15 queries e o índice de expressão) e o ganho atual é pequeno. Classificado como P3 (seção 19).

---

# 14. Prioritized Backlog

| ID | Problema | Categoria | Prio | Impacto | Esforço | Risco | Dependências |
|---|---|---|---|---|---|---|---|
| SEC-01 | Cache do frontend vaza dados entre usuários | Segurança | **P0** | Crítico | Pequeno | Baixo | — |
| SEC-02 | Rate limit por IP inoperante em serverless | Segurança | **P0** | Alto | Médio | Baixo | — |
| DATA-01 | Importação "substituir" apaga lançamentos manuais e parcelas | Integridade | **P0** | Crítico | Médio | Médio | BP-00 |
| TEST-04 | `.gitignore` impede fixtures de CSV/PDF | Testes | **P0** | Alto | Pequeno | Baixo | — |
| SEC-05 | `JWT_SECRET_KEY` ausente → segredo em claro no banco | Segurança | **P0** | Alto | Pequeno (operacional) | Baixo | — |
| DOM-02 | Taxa de juros arredondada para centavos | Domínio | **P1** | Alto | Pequeno | Baixo | BP-00 |
| DOM-03 | Compra avulsa no cartão ignora fechamento | Domínio | **P1** | Alto | Pequeno | Médio | BP-00 |
| DOM-04 | "Hoje" e "mês atual" em UTC | Domínio | **P1** | Alto | Médio | Médio | BP-00 |
| DOM-05 | Salário contado duas vezes | Domínio | **P1** | Alto | Médio | Alto | Decisão de produto |
| DOM-01 | `installment_group` como chave natural | Domínio | **P1** | Alto | Pequeno | Médio | BP-00 |
| SEC-03 | Upsert de categoria reescreve `type` | Integridade | **P1** | Alto | Pequeno | Baixo | BP-00 |
| CSV-01 | Formato `1,234.56` lido como 1,23 | CSV | **P1** | Crítico | Pequeno | Baixo | BP-00 |
| CSV-04 | Cabeçalho fora da linha 1 não suportado | CSV | **P1** | Alto | Médio | Baixo | BP-00 |
| PERF-01 | N+1 na importação → timeout | Performance | **P1** | Alto | Médio | Baixo | BP-00 |
| FIN-01 | Hash de duplicata sem hora/conta | Integridade | **P1** | Alto | Médio | Médio | BP-04 |
| UX-01 | Nenhum error boundary | Frontend | **P1** | Médio | Pequeno | Baixo | — |
| TEST-01 | Caminho cookie+CSRF nunca testado | Testes | **P1** | Alto | Médio | Baixo | — |
| CI-03 | Deploy sem passo de migration | Operação | **P1** | Alto | Pequeno | Médio | BP-08 |
| OPS-01 | DDL pesado a cada cold start | Operação | **P1** | Alto | Médio | Médio | BP-08 |
| SEC-04 | Vinculação OAuth sem confirmação | Segurança | **P1** | Alto | Médio | Médio | — |
| CI-06 | Sem error tracking | Operação | **P1** | Alto | Pequeno | Baixo | — |
| CI-07 | Sem backup verificado | Operação | **P1** | Crítico | Médio | Baixo | — |
| DEP-01 | Deps de teste em `requirements.txt` | Dependências | **P1** | Médio | Pequeno | Médio | — |
| PERF-02/03/04 | N+1 em cartões, bootstrap duplicado, conexão por query | Performance | **P1** | Alto | Médio | Médio | BP-00 |
| SEC-06 | Sem allowlist de host (open redirect) | Segurança | **P2** | Médio | Pequeno | Baixo | — |
| SEC-07 | Sessão de 7 dias sem refresh | Segurança | **P2** | Médio | Médio | Médio | — |
| SEC-08 | `pin_hash` na exportação LGPD | Conformidade | **P2** | Médio | Pequeno | Baixo | — |
| SEC-09 | Revogação de consentimento inócua | Conformidade | **P2** | Médio | Médio | Baixo | — |
| SEC-11 | Eliminação de avatar best-effort | Conformidade | **P2** | Médio | Pequeno | Baixo | — |
| DB-01 | `card_pins` sem FK composta | Banco | **P2** | Médio | Pequeno | Baixo | BP-08 |
| DB-02 | Migration `0007` ausente | Banco | **P2** | Baixo | Pequeno | Baixo | — |
| DB-03 | Duas fontes de verdade de schema | Banco | **P2** | Médio | Médio | Médio | BP-08 |
| DB-05 | `transactions` sem `updated_at` | Banco | **P2** | Médio | Pequeno | Baixo | BP-08 |
| PERF-05 | `/relatorios` com recharts estático (240 kB) | Performance | **P2** | Médio | Pequeno | Baixo | — |
| UX-02 | Drawers sem `role=dialog`/focus trap | A11y | **P2** | Médio | Pequeno | Baixo | — |
| UX-03 | Troca de senha desloga sem aviso | UX | **P2** | Médio | Pequeno | Baixo | — |
| UX-04 | Sem manifest PWA | UX | **P2** | Médio | Pequeno | Baixo | — |
| FIN-02 | Calendário diário discorda do total | Domínio | **P2** | Médio | Médio | Médio | BP-03 |
| FIN-03 | Score de reservas por nome de categoria | Domínio | **P2** | Médio | Médio | Baixo | BP-08 |
| FIN-09 | Edição livre de linha de parcelamento | Domínio | **P2** | Médio | Médio | Médio | BP-03 |
| FIN-10 | Exclusão de grupo sem aviso | UX/Domínio | **P2** | Médio | Pequeno | Baixo | — |
| CSV-02/03 | Negativo entre parênteses / sinal posposto | CSV | **P2** | Médio | Pequeno | Baixo | BP-04 |
| CSV-08 | `account` gravado em `payment_method` | CSV | **P2** | Médio | Médio | Médio | BP-08 |
| CSV-10 | Upload sem rate limit | CSV | **P2** | Médio | Pequeno | Baixo | BP-01 |
| ARCH-01 | `main.py` com 5.133 linhas | Arquitetura | **P2** | Alto | Muito grande | Alto | BP-00…BP-06 |
| DEP-02/03/04 | `lucide-react`, `freezegun`, `python-json-logger` sem uso | Dependências | **P2** | Baixo | Pequeno | Baixo | — |
| DEP-05 | `supabase` para 3 chamadas HTTP | Dependências | **P2** | Médio | Médio | Médio | — |
| DEP-06 | `passlib` abandonado prendendo `bcrypt<4.1` | Dependências | **P2** | Alto | Médio | Médio | BP-00 |
| DEP-07 | `python-jose` → `ecdsa` vulnerável | Dependências | **P2** | Médio | Médio | Médio | — |
| DEP-08 | `pydantic` sem teto + `class Config` v1 | Dependências | **P2** | Médio | Pequeno | Baixo | — |
| CI-01 | Suíte roda 2-3× por build | CI | **P2** | Baixo | Pequeno | Baixo | — |
| CI-02 | Imagem Docker publicada sem consumidor | CI | **P2** | Baixo | Pequeno | Baixo | — |
| CI-05 | Sem `npm audit` | CI | **P2** | Baixo | Pequeno | Baixo | — |
| CFG-01 | `render.yaml` quebrado | Config | **P2** | Baixo | Pequeno | Baixo | — |
| CFG-03 | Cor default da marca antiga | Config | **P2** | Baixo | Pequeno | Baixo | — |
| DEAD-01 | `errors.py`, `middleware.py`, `base.py` mortos | Limpeza | **P3** | Baixo | Pequeno | Baixo | — |
| 7.1 | Gerador de PDF à mão (420 linhas, corrompe acentos) | Limpeza | **P3** | Médio | Médio | Médio | BP-09 |
| FIN-04 | Sem transferência/estorno/pagamento de fatura | Produto | **P3** | Alto | Grande | Alto | BP-08 |
| DB-04 | Datas como `TEXT` | Banco | **P3** | Médio | Grande | Alto | BP-09 |
| TEST-02 | Sem E2E | Testes | **P3** | Médio | Médio | Baixo | BP-00 |
| — | MFA/TOTP | Segurança | **P3** | Alto | Grande | Médio | BP-05 |
| BP-12 | Open Finance via agregador | Produto | **P3** | Alto | Muito grande | Alto | Tudo acima |

---

# 15. Implementation Breakpoints

Treze breakpoints. Cada um é independente, deixa o projeto funcional ao terminar e cabe num PR.

> **Sobre os prompts de implementação:** para não duplicar texto (e não criar duas versões que divergem), o prompt autocontido de cada breakpoint está reunido na **seção 17**, referenciado por ID. Cada breakpoint abaixo aponta para o seu.

---

## BREAKPOINT 0 — Baseline e proteção contra regressões

### Objetivo
Criar a rede de segurança **antes** de mexer em dinheiro. Hoje nenhum dos P0/P1 desta auditoria tem teste que o pegue.

### Motivação
TEST-01 mostra que o caminho de autenticação de produção (cookie + CSRF) nunca é exercitado. TEST-04 mostra que fixtures de CSV não podem sequer ser versionadas. TEST-05 mostra que nenhum dos defeitos financeiros tem cobertura. Corrigir DOM-02/03/04 sem isso é apostar.

### Pré-requisitos
Nenhum.

### Escopo
- Ajustar `.gitignore` para permitir fixtures em `tests/fixtures/**` (`.csv`, `.pdf`).
- Ampliar `scripts/check-ignored-files.sh` para `.csv`, `.pdf`, `.json`, `.mjs`, `.mts`, `.yml` e corrigir o `DIRS=(...)` frágil.
- Criar `tests/fixtures/csv/` com extratos reais anonimizados: pt-BR (`1.234,56`), en-US (`1,234.56`), negativo entre parênteses, com preâmbulo antes do cabeçalho, com coluna de hora, com linhas duplicadas legítimas, `;` e `,`, UTF-8 com BOM e Windows-1252.
- Adicionar fixture `cookie_client` no `conftest.py` que autentica por **cookie + `X-CSRF-Token`** (não Bearer) e replicar ao menos 5 testes de integração existentes nesse caminho.
- Adicionar testes **falhando (xfail)** que documentem cada P0/P1: DATA-01, DOM-01, DOM-02, DOM-03, DOM-04, SEC-03, CSV-01, CSV-02, CSV-04, FIN-01, SEC-01.
- Criar `pyproject.toml` com a seção `[tool.pytest.ini_options]` (rootdir, `asyncio_mode`, marcadores).
- Rodar `npm run test:money` no CI.

### Fora de escopo
Qualquer correção de comportamento. Este breakpoint só adiciona testes e configuração de testes. Os `xfail` viram `pass` nos breakpoints seguintes.

### Arquivos/módulos envolvidos
`.gitignore` · `scripts/check-ignored-files.sh` · `tests/conftest.py` · `tests/fixtures/csv/*` · `tests/integration/*` · `tests/unit/*` · `pyproject.toml` · `.github/workflows/ci.yml`

### Tarefas
- [ ] Liberar `tests/fixtures/**` no `.gitignore` com negação explícita e comentário do porquê
- [ ] Ampliar extensões e corrigir o array em `check-ignored-files.sh`; testar que ele pega uma fixture ignorada
- [ ] Reunir/anonimizar 9 fixtures de CSV cobrindo os formatos listados
- [ ] Fixture `cookie_client` (login por cookie, `GET /api/auth/csrf`, header `X-CSRF-Token`)
- [ ] Replicar 5 testes de integração no caminho cookie
- [ ] Um `xfail` por P0/P1, com `reason` citando o ID do achado
- [ ] `pyproject.toml` com config de pytest
- [ ] Passo `npm run test:money` no CI

### Ordem interna
`.gitignore` e checker → fixtures → `cookie_client` → testes replicados → `xfail` → config → CI.

### Riscos
Fixtures com dados reais. **Anonimizar antes de versionar**: nomes, valores exatos e datas devem ser sintéticos.

### Validação
```bash
bash scripts/check-ignored-files.sh
pytest tests/ -q -rs --strict-markers
cd frontend && npm run test:money
```

### Critérios de aceite
- `check-ignored-files.sh` detecta um `.csv` propositalmente colocado fora de `tests/fixtures/`.
- As 9 fixtures estão versionadas e legíveis.
- Pelo menos 5 testes de integração passam pelo caminho cookie+CSRF.
- Existe um `xfail` com `reason` para cada ID de P0/P1 listado.
- CI verde, sem skips, com `test:money` incluído.

### Estimativa relativa
**M**

### Prompt de implementação
Ver §17 → `PROMPT BP-00`.

---

## BREAKPOINT 1 — Segurança de sessão e rate limiting

### Objetivo
Fechar SEC-01 e SEC-02 — os dois P0 de segurança — e estancar o abuso de endpoints caros.

### Motivação
Hoje, num dispositivo compartilhado, o segundo usuário vê os dados do primeiro. E nenhum limite por IP é aplicado em produção.

### Pré-requisitos
BP-00 (teste de SEC-01 existe como `xfail`).

### Escopo
- `lib/api.ts`: exportar `clearApiCache()`; chamar em `clearSession()` e após login/registro bem-sucedidos. Incluir o `id` do usuário na chave de cache quando disponível.
- Migrar os contadores de rate limit por IP para Postgres, reaproveitando o padrão de `login_failures_state` (nova tabela `rate_limit_state` com `key_hash`, `window_start`, `count`).
- Adicionar rate limit a `POST /api/imports/csv/upload`.
- Montar `TrustedHostMiddleware` com allowlist derivada de `ALLOWED_ORIGINS` + `TRUSTED_HOSTS` (SEC-06).
- Remover ou documentar as anotações `@limiter.limit` que não puderem ser migradas, para não criarem falsa sensação de proteção.

### Fora de escopo
OAuth (BP-05), TTL de sessão (BP-05), MFA.

### Arquivos/módulos envolvidos
`frontend/lib/api.ts` · `frontend/lib/authSession.ts` · `frontend/app/login/page.tsx` · `frontend/app/cadastro/page.tsx` · `frontend/app/configuracoes/page.tsx` · `app/main.py` (limiter e middlewares) · `migrations/00NN_rate_limit_state.sql`

### Tarefas
- [ ] `clearApiCache()` exportada e chamada em logout, login e registro
- [ ] Chave do cache passa a incluir o `id` do usuário autenticado
- [ ] Migration `rate_limit_state`
- [ ] Limiter persistido em Postgres, com degradação graciosa se o banco cair (fail-open com log, nunca fail-closed no login)
- [ ] Rate limit no upload de CSV
- [ ] `TrustedHostMiddleware` + variável `TRUSTED_HOSTS` documentada no `.env.example`
- [ ] Teste: cache limpo após logout (o `xfail` de SEC-01 vira `pass`)
- [ ] Teste: 6ª tentativa de cadastro no mesmo IP recebe 429 mesmo com processo reiniciado

### Ordem interna
Cache do frontend (isolado, ganho imediato) → migration → limiter → `TrustedHostMiddleware` → testes.

### Riscos
- Limiter persistido adiciona uma query por request limitado; manter o conjunto de rotas pequeno.
- `TrustedHostMiddleware` mal configurado derruba produção. Derivar a allowlist do ambiente e **incluir teste** de que a origem de produção passa.

### Validação
```bash
pytest tests/integration/test_auth.py tests/unit/test_security.py -q
cd frontend && npx tsc --noEmit && npm run lint
```
Manual: login como A, logout, login como B na mesma aba, conferir que o dashboard é de B.

### Critérios de aceite
- Após logout, `responseCache` está vazio (asserção em teste ou verificação manual documentada).
- Limite de cadastro é respeitado através de reinício de processo.
- Host não listado recebe 400 do `TrustedHostMiddleware`; host de produção passa.
- Nenhuma rota expõe `@limiter.limit` sem backend persistente.

### Estimativa relativa
**M**

### Prompt de implementação
Ver §17 → `PROMPT BP-01`.

---

## BREAKPOINT 2 — Integridade de dados e operações destrutivas

### Objetivo
Impedir que o produto apague ou corrompa dados do usuário: DATA-01, DOM-01, SEC-03, FIN-10.

### Motivação
São as quatro operações que podem destruir histórico financeiro sem possibilidade de recuperação.

### Pré-requisitos
BP-00.

### Escopo
- **DATA-01:** migration adicionando `transactions.import_batch_id UUID`. O modo "replace" passa a ser **"desfazer última importação"**: apaga apenas `source = 'csv_import'` do lote anterior. Se a substituição por mês tiver de continuar existindo, restringir a `source = 'csv_import' AND installment_group IS NULL`.
- **DOM-01:** `installment_group` passa a ser `str(uuid4())` nos dois pontos de criação. Script de verificação (somente leitura) que detecta grupos já colididos em produção.
- **SEC-03:** `create_category` responde 409 quando existe categoria **ativa** de mesmo nome; o upsert passa a reativar arquivada sem alterar `type`.
- **FIN-10:** `DELETE /api/transactions/{id}` de uma linha com `installment_group` exige `?scope=group` explícito; sem ele, responde 409 informando quantas parcelas seriam afetadas. Frontend passa a confirmar.
- Texto do `ImportConfirmDialog` reescrito para dizer exatamente o que será apagado (UX-07).

### Fora de escopo
Hash de duplicata (BP-04), performance da importação (BP-04), parsing (BP-04).

### Arquivos/módulos envolvidos
`app/main.py` (`confirm_csv_import`, `create_installments`, `create_installments_without_card`, `create_category`, `delete_transaction`) · `migrations/00NN_import_batch.sql` · `frontend/components/ImportConfirmDialog.tsx` · `frontend/app/transacoes/page.tsx` · `frontend/lib/api.ts`

### Tarefas
- [ ] Migration `import_batch_id` + índice `(user_id, import_batch_id)`
- [ ] `confirm_csv_import` grava `import_batch_id` em todas as linhas do lote
- [ ] Modo destrutivo restrito e/ou substituído por "desfazer última importação"
- [ ] `installment_group` vira UUID nos dois pontos de criação
- [ ] Script `scripts/check-installment-groups.sql` (somente leitura) para auditar colisões existentes
- [ ] `create_category` com 409 para nome ativo; reativação sem mudar `type`
- [ ] `delete_transaction` exige `scope=group`; frontend confirma com contagem
- [ ] Copy do diálogo de importação reescrita
- [ ] `xfail` de DATA-01, DOM-01, SEC-03 viram `pass`

### Ordem interna
Migration → `import_batch_id` na importação → modo destrutivo → `installment_group` UUID → categoria → exclusão de grupo → frontend.

### Riscos
- Mudar `installment_group` não afeta dados existentes, mas o script de auditoria pode revelar colisões já ocorridas — o relatório deve ser entregue ao usuário, não corrigido automaticamente.
- Restringir o `DELETE` do "replace" muda comportamento observável: documentar no CHANGELOG.

### Validação
```bash
pytest tests/integration/test_csv_import.py tests/integration/test_cards.py tests/integration/test_transactions.py -q
psql "$DATABASE_URL" -f scripts/check-installment-groups.sql
```

### Critérios de aceite
- Importar um CSV do mês M com um lançamento manual existente em M **preserva** o lançamento manual (teste).
- Importar duas vezes a mesma compra parcelada cria **dois** grupos distintos (teste).
- Criar categoria com nome de categoria ativa responde 409 e o `type` original permanece (teste).
- `DELETE` de parcela sem `scope=group` responde 409 com a contagem (teste).

### Estimativa relativa
**M**

### Prompt de implementação
Ver §17 → `PROMPT BP-02`.

---

## BREAKPOINT 3 — Correções de domínio financeiro

### Objetivo
Fazer os números do produto ficarem corretos: DOM-02, DOM-03, DOM-04, DOM-05, FIN-02, FIN-09.

### Motivação
O produto responde "quanto posso gastar hoje" com um número que pode estar errado por fuso, por dupla contagem de salário, ou por fatura no mês errado.

### Pré-requisitos
BP-00. DOM-05 exige **decisão de produto** antes de começar (ver Riscos).

### Escopo
- **DOM-04:** criar `app/shared/clock.py` com `today()`, `now()` e `current_month()` num fuso configurável (`APP_TIMEZONE`, default `America/Sao_Paulo`). Substituir **todos** os `datetime.now(UTC).date()` e `get_current_month()` de decisão de negócio. Timestamps de persistência continuam em UTC.
- **DOM-03:** `create_transaction` chama `first_billing_month(transaction_date, card.closing_day)` quando `cardId` é informado e `billingMonth` não vem explícito.
- **DOM-02:** taxa de juros deixa de passar por `round_money`; usar `to_decimal(str(rate)) / 100` com precisão plena. Extrair a fórmula duplicada (`simulate_installments` e `create_installments_without_card`) para `app/shared/money.py::apply_installment_interest`.
- **DOM-05:** implementar a decisão tomada (recomendação: `renda_efetiva = max(monthly_income, inflow_de_categorias_de_salário)`) num único helper usado por `get_dashboard`, `_compute_goals` e `calculate_score`. Adicionar aviso na UI quando `inflow` já cobre `monthly_income`.
- **FIN-02:** alinhar o filtro da série diária de `_compute_goals` com o filtro do total do mês.
- **FIN-09:** bloquear alteração de `type`, `billing_month` e `amount` em linhas com `installment_group` (409 com mensagem clara); recalcular `duplicate_hash` quando um lançamento importado é editado.

### Fora de escopo
Transferência, estorno e conta bancária (FIN-04, adiado para depois de BP-08). Data de competência (FIN-05).

### Arquivos/módulos envolvidos
`app/shared/clock.py` (novo) · `app/shared/dates.py` · `app/shared/money.py` · `app/main.py` (`create_transaction`, `update_transaction`, `_compute_goals`, `get_dashboard`, `calculate_score`, `simulate_installments`, `create_installments_without_card`) · `app/core/config.py` · `.env.example`

### Tarefas
- [ ] `app/shared/clock.py` com `APP_TIMEZONE`
- [ ] Substituir todos os usos de "agora" de negócio; `grep -n "now(UTC)" app/` deve sobrar só em timestamps de persistência
- [ ] `first_billing_month` aplicado em `create_transaction`
- [ ] `apply_installment_interest` extraída, sem `round_money` na taxa, sem `Decimal(float)`
- [ ] Helper único de renda efetiva, com a decisão de produto documentada em comentário
- [ ] Alinhar filtro da série diária com o do total
- [ ] Guardas em `update_transaction` para linhas de parcelamento
- [ ] Recalcular `duplicate_hash` em edição de lançamento importado
- [ ] `xfail` de DOM-02/03/04/05 viram `pass`

### Ordem interna
`clock.py` primeiro (toca o maior número de funções) → `first_billing_month` → juros → renda efetiva → FIN-02 → FIN-09.

### Riscos
- **DOM-05 é uma decisão de produto, não técnica.** Implementar a opção errada muda o número principal do produto para todos os usuários. Decidir e documentar **antes** de codar.
- Mudar o fuso altera o `progress_day` e portanto a meta diária exibida — mudança visível. Anunciar no CHANGELOG.
- `first_billing_month` em `create_transaction` muda o mês de lançamentos novos no crédito. Não deve tocar lançamentos existentes.

### Validação
```bash
pytest tests/unit/test_finance_service.py tests/unit/test_card_service.py tests/integration/ -q
python -c "from app.shared.clock import today, current_month; print(today(), current_month())"
grep -rn "datetime.now(UTC)" app/ | grep -v "persist\|created_at\|audit"
```

### Critérios de aceite
- Com o relógio em 30/09 23h30 BRT, `current_month()` devolve `2026-09` (teste com `freezegun` ou injeção de relógio).
- Compra no crédito após o fechamento cai no mês seguinte (teste, tanto avulsa quanto parcelada).
- Taxa de 0,4 % a.m. produz juros maiores que zero (teste).
- Somar as barras diárias do calendário de metas dá exatamente `outflowToToday` (teste).
- Tentar mudar `type` de uma linha de parcelamento responde 409 (teste).

### Estimativa relativa
**L**

### Prompt de implementação
Ver §17 → `PROMPT BP-03`.

---

## BREAKPOINT 4 — CSV: correção, robustez e performance

### Objetivo
Fazer a importação funcionar com extratos bancários brasileiros reais, sem perder valor nem estourar timeout: CSV-01 a CSV-11, PERF-01, FIN-01.

### Motivação
A importação de CSV é o recurso que mais diferencia o produto e o que tem mais defeitos concretos. Hoje um extrato em formato en-US divide valores por 1000, um extrato de Itaú não é sequer lido, e um arquivo grande não termina.

### Pré-requisitos
BP-00 (fixtures), BP-02 (modo destrutivo já corrigido).

### Escopo
- **CSV-01:** regra do último separador em `parse_decimal_text`.
- **CSV-02/03:** parênteses como negativo; sinal posposto.
- **CSV-04:** detectar a linha de cabeçalho (primeira linha cujas células, em maioria, não são numéricas e cujo número de colunas se repete nas linhas seguintes). Permitir que o usuário escolha a linha de cabeçalho no preview.
- **CSV-05:** detectar `\t` e `|` além de `;` e `,` (usar `csv.Sniffer` com fallback à heurística atual).
- **CSV-06:** `cp1252` antes de `latin-1`.
- **CSV-07:** remover o apóstrofo de guarda na leitura.
- **PERF-01:** carregar categorias e regras **uma vez por importação**; substituir o `SELECT` de duplicata por linha por `INSERT ... ON CONFLICT (user_id, duplicate_hash) DO NOTHING` com `execute_values`.
- **CSV-11:** `confirm` reaproveita o resultado do `preview` (guardá-lo na sessão) em vez de recomputar.
- **FIN-01:** incluir hora e conta no `duplicate_hash` quando existirem; manter o hash antigo como `legacy` (o código já tem esse padrão) para não invalidar dedup histórica.
- **CSV-08/09:** `account` vai para coluna própria (`transactions.account`, nova) em vez de `payment_method`; `external_id` deixa de receber o hash.
- **CSV-10:** rate limit no upload (feito em BP-01; só confirmar).

### Fora de escopo
Perfis de banco salvos (mapeamento persistido por instituição) — melhoria desejável, não requisito. OFX. Open Finance.

### Arquivos/módulos envolvidos
`app/integrations/normalizer.py` · `app/main.py` (`parse_csv_rows`, `detect_csv_delimiter`, `resolve_import_category`, `build_csv_import_preview`, `upload_csv_import`, `confirm_csv_import`) · `migrations/00NN_transaction_account.sql` · `frontend/app/importar/page.tsx` · `tests/integration/test_csv_import.py`

### Tarefas
- [ ] `parse_decimal_text`: último separador manda; parênteses e sinal posposto
- [ ] `parse_csv_rows`: detecção de linha de cabeçalho + `csv.Sniffer` para delimitador + `cp1252`
- [ ] Preview permite escolher a linha de cabeçalho
- [ ] Categorias e regras carregadas uma vez por importação
- [ ] Inserção em lote com `ON CONFLICT DO NOTHING`
- [ ] `confirm` reusa o preview guardado na sessão
- [ ] `duplicate_hash` com hora e conta; `legacy` preservado
- [ ] Migration `transactions.account`; `payment_method` volta a significar forma de pagamento
- [ ] `external_id` não recebe mais o hash
- [ ] Desfazer o `csv_safe_cell` na leitura
- [ ] Um teste por fixture do BP-00

### Ordem interna
Parsing puro (`normalizer.py`, testável isoladamente) → detecção de cabeçalho/delimitador/encoding → performance → hash → colunas.

### Riscos
- **Mudar o `duplicate_hash` pode reimportar transações já importadas.** Por isso o hash antigo continua sendo consultado como `legacy` — esse mecanismo já existe no código e deve ser preservado.
- Detecção automática de cabeçalho pode errar; por isso o preview deve permitir correção manual.
- A regra do último separador muda a interpretação de arquivos já importados — mas só afeta importações futuras.

### Validação
```bash
pytest tests/integration/test_csv_import.py -q -v
# performance:
python -m timeit -n1 -r1 "…preview de fixture com 3000 linhas…"
```

### Critérios de aceite
- Cada uma das 9 fixtures do BP-00 importa com valores, sinais e datas corretos (teste por fixture).
- `"1,234.56"` vira `1234.56` e `"1.234,56"` vira `1234.56` (teste).
- `"(123,45)"` vira despesa de 123,45 (teste).
- Fixture com preâmbulo de 4 linhas é lida corretamente.
- Preview de 3.000 linhas faz no máximo ~10 queries (contar com um hook no cursor) e completa em menos de 5 s localmente.
- Duas transações legítimas idênticas no mesmo dia com horas diferentes são ambas importadas.

### Estimativa relativa
**L**

### Prompt de implementação
Ver §17 → `PROMPT BP-04`.

---

## BREAKPOINT 5 — Superfície de autenticação e OAuth

### Objetivo
Fechar SEC-04, SEC-05, SEC-07 e as lacunas de conformidade SEC-08, SEC-09, SEC-11.

### Motivação
A vinculação social silenciosa transfere a segurança da conta financeira para a política de verificação de terceiros, sem defesa em profundidade. O segredo de assinatura pode estar em claro no banco. A revogação de consentimento não produz efeito.

### Pré-requisitos
BP-01 (`TrustedHostMiddleware` já montado).

### Escopo
- **SEC-04:** `resolve_oauth_user` deixa de vincular automaticamente. Se existe conta com o e-mail e ela tem `hashed_password`, responder erro de "conta já existe — entre com sua senha e vincule no perfil". Criar rota autenticada `POST /api/auth/oauth/{provider}/link`.
- **SEC-05:** logar `WARNING` em produção quando o segredo vier de `app_secrets`; expor o estado em `/api/health`; documentar a rotação. Cadastrar `JWT_SECRET_KEY` no ambiente da Vercel é ação operacional (fora do código, mas parte do critério de aceite).
- **SEC-07:** reduzir `ACCESS_TOKEN_EXPIRE_HOURS` para 24 e adicionar renovação deslizante (reemitir o cookie em requests autenticados quando faltar menos de 25 % da validade).
- **SEC-08:** adicionar `card_pins` a `_SKIP_TABLES`, ou exportar a tabela sem a coluna `pin_hash`.
- **SEC-09:** revogar `terms_privacy` passa a iniciar o fluxo de exclusão (ou, no mínimo, desativar a conta e informar o prazo), em vez de ser no-op.
- **SEC-11:** falha em remover o avatar passa a ser registrada numa tabela de pendências de eliminação, com reprocessamento manual documentado.
- **SEC-12:** parar de devolver `access_token` no corpo de `login`/`register` quando a origem for o SPA (manter para clientes Bearer, via header `Accept` ou parâmetro explícito).

### Fora de escopo
MFA/TOTP (P3 — ver seção 19). Múltiplos provedores por usuário (melhoria desejável).

### Arquivos/módulos envolvidos
`app/main.py` (`resolve_oauth_user`, `oauth_callback`, `login`, `register`, `update_consent`, `delete_account`, `health`) · `app/oauth.py` · `app/core/signing.py` · `app/core/config.py` · `app/privacy/service.py` · `frontend/app/perfil/page.tsx` · `frontend/components/auth/SocialLoginButtons.tsx`

### Tarefas
- [ ] `resolve_oauth_user` não vincula automaticamente a conta com senha
- [ ] Rota autenticada de vinculação + UI no perfil
- [ ] `WARNING` + sinal em `/api/health` quando o segredo vier do banco
- [ ] `JWT_SECRET_KEY` cadastrada no ambiente de produção
- [ ] TTL de 24 h + renovação deslizante
- [ ] `card_pins` fora da exportação LGPD (ou sem `pin_hash`)
- [ ] Revogação de `terms_privacy` com efeito real
- [ ] Registro de pendências de eliminação de avatar
- [ ] Token fora do corpo da resposta para o SPA

### Ordem interna
OAuth linking → segredo → TTL → LGPD (três itens) → corpo da resposta.

### Riscos
- **Mudar o TTL desloga todo mundo** na transição. Anunciar.
- Bloquear a vinculação automática pode quebrar o login de usuários que hoje entram por social numa conta criada por senha — hoje são 13 usuários; verificar quantos estão nessa situação antes.
- Revogação de consentimento com efeito de exclusão é irreversível: exigir confirmação dupla.

### Validação
```bash
pytest tests/unit/test_oauth.py tests/unit/test_privacy_service.py tests/integration/test_auth.py -q
curl -s $BASE/api/health | jq '.checks'
```

### Critérios de aceite
- Callback OAuth com e-mail de conta existente com senha **não** autentica; devolve erro orientando a vincular pelo perfil (teste).
- Vinculação pelo perfil, autenticada, funciona (teste).
- `/api/health` indica se o segredo veio do ambiente ou do banco.
- `GET /api/privacy/export` não contém `pin_hash` (teste).
- Sessão expira em 24 h e é renovada em uso.

### Estimativa relativa
**L**

### Prompt de implementação
Ver §17 → `PROMPT BP-05`.

---

## BREAKPOINT 6 — Performance de banco e do bootstrap

### Objetivo
Eliminar PERF-02, PERF-03, PERF-04, PERF-07 — os N+1 que sobraram e o custo de conexão em serverless.

### Motivação
`/api/bootstrap` é o endpoint que toda tela chama. Hoje ele executa `get_cards_summary` (que é N+1) duas vezes, `calculate_score` duas vezes, e abre dezenas de conexões novas.

### Pré-requisitos
BP-00.

### Escopo
- Consolidar `get_cards_summary` em 2 queries totais (uma agregação de fatura por cartão, uma agregação de parcelas por grupo), independentemente do número de cartões.
- Estender `request_cached` para `get_settings`, `list_categories`, `list_cards`, `get_cards_summary`, `get_invoice_total`, `calculate_score`.
- Consolidar os laços de `calculate_score` e `get_alerts_for_month` numa agregação por cartão.
- `simulate_card_invoices`: uma query com `GROUP BY` em vez de uma por mês.
- Introduzir uma conexão por request em serverless: um `ContextVar` que guarda a conexão aberta e a fecha no fim do request, em vez de abrir uma por `db_cursor()`.
- Medir antes e depois (contador de queries e de conexões num middleware de debug).

### Fora de escopo
Migração de `TEXT` para `DATE` (P3). Cache entre requests. Réplica de leitura.

### Arquivos/módulos envolvidos
`app/core/database.py` · `app/main.py` (`get_cards_summary`, `get_card_commitment`, `calculate_score`, `get_alerts_for_month`, `simulate_card_invoices`, `bootstrap`, `request_cached`)

### Tarefas
- [ ] Middleware de debug que conta queries e conexões por request (atrás de env var)
- [ ] Medir `/api/bootstrap` com 3 cartões e 20 grupos de parcelas — número de partida
- [ ] `get_cards_summary` em 2 queries
- [ ] `request_cached` estendido aos 6 produtores
- [ ] Laços por cartão em `calculate_score` e `get_alerts_for_month` consolidados
- [ ] `simulate_card_invoices` com `GROUP BY`
- [ ] Conexão por request em serverless
- [ ] Medir de novo; registrar antes/depois no PR

### Ordem interna
Instrumentação → medir → `get_cards_summary` → `request_cached` → laços restantes → conexão por request → medir.

### Riscos
- `request_cached` em `get_cards_summary` pode servir dado obsoleto **dentro** de um request que também escreve. Nenhum endpoint atual escreve e relê cartões no mesmo request — verificar antes.
- Conexão por request muda a semântica transacional: hoje cada `db_cursor(commit=True)` é uma transação isolada. Com conexão compartilhada, um `rollback` afeta o que veio antes. **Manter `commit=True` como commit explícito e testar as rotas de escrita.**

### Validação
```bash
TREVO_DEBUG_QUERIES=1 pytest tests/integration/test_bootstrap.py -q -s
pytest tests/integration/ -q
```

### Critérios de aceite
- `/api/bootstrap` com 3 cartões e 20 grupos faz **menos de 20 queries** (hoje: ~150).
- Em serverless, um request abre **1 conexão** (asserção via contador).
- Toda a suíte de integração continua verde.
- Números antes/depois registrados na descrição do PR.

### Estimativa relativa
**L**

### Prompt de implementação
Ver §17 → `PROMPT BP-06`.

---

## BREAKPOINT 7 — Frontend: boundaries, bundle e acessibilidade

### Objetivo
UX-01, UX-02, UX-03, UX-04, PERF-05, PERF-06, PERF-08.

### Motivação
Um erro de render hoje mostra a tela padrão do Next. `/relatorios` carrega o dobro de JS de qualquer outra página. Dois drawers não seguem o padrão de diálogo acessível que o próprio projeto já estabeleceu.

### Pré-requisitos
Nenhum (independente do backend).

### Escopo
- `app/error.tsx`, `app/global-error.tsx`, `app/not-found.tsx` com a identidade Trevo e caminho de recuperação.
- `/relatorios`: extrair o gráfico de barras para `components/charts/ReportBarChart.tsx` e carregar com `dynamic()` + `ChartSkeleton` de altura fixa, replicando o padrão de `SummaryHome.tsx`.
- `CreateCategoryDrawer` e `FinancialPlanningDrawer`: extrair o padrão de `ImportConfirmDialog` para um `components/Dialog.tsx` reutilizável (focus trap, `Escape`, `aria-modal`, restauração de foco) e aplicá-lo aos três.
- Handle de resize da sidebar: `role="separator"`, `aria-orientation="vertical"`, `aria-valuenow/min/max`.
- `URL.revokeObjectURL` após o download.
- Fontes: definir `unicodeRange` nas duas faces, ou remover `latin-ext` se não for necessária para pt-BR (medir o impacto no bundle).
- `public/manifest.webmanifest` + `theme-color` + ícones maskable; ligar no `metadata` do layout.
- Após troca de senha, mostrar mensagem explícita e redirecionar com contexto ("sua senha mudou, entre novamente").
- Regiões de mensagem com `aria-live="polite"`.

### Fora de escopo
Redesign. Novos componentes de produto. Service worker / offline.

### Arquivos/módulos envolvidos
`frontend/app/error.tsx` · `global-error.tsx` · `not-found.tsx` (novos) · `frontend/app/relatorios/page.tsx` · `frontend/components/charts/ReportBarChart.tsx` (novo) · `frontend/components/Dialog.tsx` (novo) · `ImportConfirmDialog.tsx` · `CreateCategoryDrawer.tsx` · `FinancialPlanningDrawer.tsx` · `AppShell.tsx` · `frontend/app/layout.tsx` · `frontend/public/manifest.webmanifest` · `frontend/app/perfil/page.tsx`

### Tarefas
- [ ] Três arquivos de boundary com identidade e botão de recuperação
- [ ] `ReportBarChart` extraído + `dynamic()` + skeleton de altura exata
- [ ] `components/Dialog.tsx` extraído de `ImportConfirmDialog` e aplicado aos três diálogos
- [ ] ARIA no handle de resize
- [ ] `revokeObjectURL`
- [ ] Decisão sobre `latin-ext` + `unicodeRange`
- [ ] Manifest PWA + `theme-color`
- [ ] Mensagem de troca de senha
- [ ] `aria-live` nas regiões de mensagem

### Ordem interna
Boundaries (isolado) → `Dialog.tsx` → aplicar aos três → `/relatorios` → fontes → manifest → ARIA e detalhes.

### Riscos
- Extrair `Dialog.tsx` pode regredir o focus trap do `ImportConfirmDialog`, que hoje funciona. Testar manualmente os três com teclado antes de fechar.
- Carregar o gráfico de relatórios sob demanda pode gerar CLS se o skeleton não tiver a altura exata — o comentário em `ChartSkeleton.tsx` já explica isso.

### Validação
```bash
cd frontend && npx tsc --noEmit && npm run lint && npm run build
```
Conferir na tabela do build que `/relatorios` caiu abaixo de 140 kB. Navegar os três diálogos só com teclado.

### Critérios de aceite
- `/relatorios` com First Load JS **abaixo de 140 kB** (hoje 240 kB), verificado na saída do `next build`.
- Um erro lançado propositalmente numa página mostra a tela de erro do Trevo, com botão de voltar.
- Os três diálogos: abrem com foco no primeiro elemento, prendem Tab, fecham com `Escape`, devolvem foco à origem.
- `manifest.webmanifest` válido; app instalável no Chrome mobile.

### Estimativa relativa
**M**

### Prompt de implementação
Ver §17 → `PROMPT BP-07`.

---

## BREAKPOINT 8 — Schema e migrations

### Objetivo
Congelar o baseline, eliminar a dupla fonte de verdade e fechar DB-01 a DB-06, OPS-01, CI-03.

### Motivação
Hoje o schema vive em dois lugares, o DDL pesado roda a cada cold start, e o deploy não aplica migrations.

### Pré-requisitos
BP-02 e BP-04 (que adicionam colunas — evita duas rodadas de migration no mesmo período).

### Escopo
- Extrair `SCHEMA_SQL` para `migrations/0000_baseline.sql`; registrar `0000` como aplicado nos bancos existentes; remover `SCHEMA_SQL` de `migrate.py`.
- `ensure_serverless_schema` passa a **verificar** (`SELECT` em `schema_migrations`) e alertar, nunca aplicar.
- `vercel.json`: `buildCommand` que executa `python migrate.py` (ou script de deploy equivalente documentado em `docs/guides/deployment.md`).
- `card_pins`: adicionar FK composta `(user_id, card_id) REFERENCES cards(user_id, id) ON DELETE CASCADE`.
- `transactions.updated_at` + trigger `set_updated_at`.
- `categories.role TEXT` (`reserve`, `investment`, `income`, `other`) e migração dos defaults; `calculate_score` passa a usar `role` em vez de nome literal (FIN-03).
- Investigar e documentar a ausência de `0007`: verificar `schema_migrations` em produção e registrar a conclusão em `migrations/README.md`.
- Documentar as três colunas de reserva (DB-06) num comentário de schema.
- Paginação em `/api/transactions` (DB-08): `limit`/`offset` com `hasMore`.

### Fora de escopo
`transaction_date` de `TEXT` para `DATE` (P3, seção 19). Transferência/estorno (P3).

### Arquivos/módulos envolvidos
`migrate.py` · `migrations/0000_baseline.sql` (novo) · `migrations/00NN_*.sql` (novas) · `migrations/README.md` (novo) · `app/main.py` (`ensure_serverless_schema`, `calculate_score`, `list_transactions`, `transactions`) · `vercel.json` · `docs/guides/deployment.md`

### Tarefas
- [ ] `0000_baseline.sql` a partir do `SCHEMA_SQL` atual, byte a byte
- [ ] Script de marcação idempotente que registra `0000` em bancos que já têm o schema
- [ ] `SCHEMA_SQL` removida de `migrate.py`
- [ ] `ensure_serverless_schema` só verifica
- [ ] `buildCommand` de migration na Vercel + documentação
- [ ] Migration: FK composta em `card_pins`
- [ ] Migration: `transactions.updated_at` + trigger
- [ ] Migration: `categories.role` + backfill dos defaults
- [ ] `calculate_score` usa `role`
- [ ] Paginação em `/api/transactions`
- [ ] `migrations/README.md` com a convenção e a conclusão sobre `0007`

### Ordem interna
Baseline e marcação (o mais delicado, fazer primeiro e validar num banco descartável) → `ensure_serverless_schema` → passo de deploy → migrations novas → código que as usa.

### Riscos
- **Este é o breakpoint de maior risco operacional.** Marcar `0000` errado num banco existente faz o próximo deploy tentar recriar o schema. Testar contra um dump restaurado de produção **antes** de aplicar.
- Adicionar FK composta em `card_pins` falha se houver linha inconsistente. Rodar uma query de verificação antes e incluí-la na migration como `DO $$ ... RAISE EXCEPTION ... $$` explícito.
- `ALTER TABLE transactions ADD COLUMN updated_at ... DEFAULT NOW()` é barato no Postgres 11+ (não reescreve a tabela). Confirmar a versão.

### Validação
```bash
# contra um banco descartável restaurado de um dump de produção:
python migrate.py
psql "$DATABASE_URL" -c "SELECT version FROM schema_migrations ORDER BY version"
psql "$DATABASE_URL" -c "\d+ card_pins"
pytest tests/ -q
```

### Critérios de aceite
- `SCHEMA_SQL` não existe mais em `migrate.py`.
- Num banco já existente, `python migrate.py` não aplica nada e não erra.
- Num banco vazio, `python migrate.py` cria o schema completo a partir de `0000` + versionadas.
- `ensure_serverless_schema` não executa DDL (verificável com o contador de queries do BP-06).
- `\d card_pins` mostra a FK composta.
- Deploy na Vercel executa as migrations no build.

### Estimativa relativa
**L**

### Prompt de implementação
Ver §17 → `PROMPT BP-08`.

---

## BREAKPOINT 9 — Extração de roteadores por domínio

### Objetivo
Quebrar `app/main.py` (5.133 linhas) na estrutura-alvo da seção 13, sem mudar comportamento.

### Motivação
Todos os breakpoints anteriores tocaram o mesmo arquivo. Enquanto ele existir, cada mudança é um conflito em potencial, nenhuma fronteira é testável isoladamente e revisão de PR é inviável.

### Pré-requisitos
**BP-00 a BP-06.** Extrair antes de corrigir significaria mover código errado e depois corrigi-lo em dois lugares.

### Escopo
Refatoração **estritamente mecânica**, um domínio por commit:

1. `app/api/middleware.py` + `app/api/deps.py` (middlewares, `get_current_user`, `PlainDictRoute`, `request_cached`)
2. `app/auth/` (register, login, logout, me, change-password, stats, oauth, delete-account)
3. `app/privacy/router.py` (export, consent)
4. `app/categories/`
5. `app/cards/` (inclui PIN, unlock, simulação, parcelas com cartão)
6. `app/transactions/` (inclui parcelas sem cartão, recorrência)
7. `app/budgets/` (inclui regras de categorização)
8. `app/goals/`
9. `app/imports/` (inclui `parsers/`)
10. `app/reports/` (inclui `pdf.py` — isolar o gerador de 420 linhas)
11. `app/dashboard/` (composição de leitura: bootstrap, score, alerts, suggestions)
12. `main.py` final: app, middlewares, `include_router`, lifespan

Cada domínio: `router.py` (rotas), `service.py` (regras), `repository.py` (SQL), `schemas.py` (Pydantic).

### Fora de escopo
**Qualquer mudança de comportamento.** Nenhuma correção de bug, nenhuma otimização, nenhuma mudança de assinatura de API. Se aparecer um bug durante a extração, anotar e tratar depois.

### Arquivos/módulos envolvidos
Todo `app/`.

### Tarefas
- [ ] Um commit por domínio, na ordem acima
- [ ] Após cada commit: suíte completa verde
- [ ] Após cada commit: comparar `openapi.json` gerado com o baseline capturado antes de começar
- [ ] `pdf.py` isolado em `app/reports/`
- [ ] Remover `app/core/errors.py` e `app/core/middleware.py`
- [ ] Documentar as regras de dependência entre módulos em `docs/architecture/overview.md`
- [ ] `main.py` final com menos de 200 linhas

### Ordem interna
A listada. Começar por `deps`/`middleware` (todos dependem), terminar por `dashboard` (depende de todos).

### Riscos
- **Importação circular** é o risco principal. A regra de dependência da seção 13 existe para evitá-lo; `dashboard` e `reports` são os únicos que importam outros domínios, e só os `service`.
- Estado global compartilhado (`csv_import_sessions`, `card_unlock_sessions`, `revoked_token_hashes`, `login_failures`) precisa de um lar. Sugestão: `app/core/ephemeral.py` com o aviso de que são fallbacks de processo.
- Mudança acidental de comportamento. A comparação de `openapi.json` a cada commit é a defesa.

### Validação
```bash
python -c "import json,urllib.request; …"  # capturar openapi.json antes
pytest tests/ -q
python -c "from app.main import app; import json; print(len(app.routes))"
diff <(jq -S . openapi-antes.json) <(jq -S . openapi-depois.json)
ruff check .
```

### Critérios de aceite
- `wc -l app/main.py` < 200.
- `openapi.json` idêntico ao baseline (mesmas 56 rotas, mesmos métodos e parâmetros).
- Suíte completa verde em cada commit intermediário.
- Nenhuma pasta de domínio com `__init__.py` vazio.
- Regras de dependência documentadas e respeitadas (verificável por `grep` de imports cruzados).

### Estimativa relativa
**XL**

### Prompt de implementação
Ver §17 → `PROMPT BP-09`.

---

## BREAKPOINT 10 — Dependências e limpeza técnica

### Objetivo
DEP-01 a DEP-08, DEAD-01, CFG-01 a CFG-06, DOC-01, 7.1, 7.2, 7.3.

### Motivação
Dependências de teste estão no bundle de produção. A biblioteca de hash de senha está congelada em 2023 por causa de um wrapper abandonado. Há três arquivos mortos e três resíduos da marca anterior.

### Pré-requisitos
BP-09 (o gerador de PDF já isolado em `app/reports/pdf.py`, o que torna a substituição trivial).

### Escopo
- Separar `requirements.txt` (produção) de `requirements-dev.txt` (teste/lint). Atualizar CI, Dockerfile e `.env.example`.
- Trocar `passlib` por uso direto de `bcrypt`, liberando `bcrypt` para a versão atual. Manter compatibilidade com hashes existentes (o formato `$2b$` é o mesmo).
- Trocar `python-jose` por `PyJWT`, eliminando `ecdsa` e `rsa` da árvore.
- Fixar teto de `pydantic` (`>=2.9,<3`) e migrar `class Config` → `model_config = ConfigDict(extra="forbid")` nos 22 modelos.
- Remover `lucide-react`, `freezegun`, `python-json-logger`.
- Substituir o cliente `supabase` por três chamadas `httpx` à Storage REST API.
- Substituir o gerador de PDF à mão por `fpdf2` (resolve a corrupção de acentos).
- Remover `app/core/errors.py` e `app/core/middleware.py`; mover `app/integrations/base.py` para documentação ou marcá-lo como especificação.
- Remover `render.yaml`. Tornar `build-and-push` condicional a tag de release, ou removê-lo.
- Renomear `ritmo` → `trevo` em `docker-compose.yml` e no CI.
- Corrigir `CategoryPayload.color` para `#2E9D5B`.
- Unificar a versão `2.0.0` numa constante única; publicar a primeira tag no `CHANGELOG.md`.
- Corrigir `Makefile lint` para `ruff check .`.
- Corrigir `open_finance/README.md` (DOC-01).

### Fora de escopo
Mudanças de comportamento de produto.

### Arquivos/módulos envolvidos
`requirements.txt` · `requirements-dev.txt` (novo) · `requirements-lock.txt` · `Dockerfile` · `.github/workflows/ci.yml` · `app/core/security.py` · `app/core/signing.py` · `app/oauth.py` · `app/core/storage.py` · `app/reports/pdf.py` · `frontend/package.json` · `docker-compose.yml` · `render.yaml` (remoção) · `Makefile` · `CHANGELOG.md`

### Tarefas
- [ ] Split de requirements + CI + Dockerfile
- [ ] `passlib` → `bcrypt` direto (com teste de que hashes antigos continuam verificando)
- [ ] `python-jose` → `PyJWT` (com teste de que tokens antigos continuam válidos)
- [ ] Teto de `pydantic` + `ConfigDict` nos 22 modelos
- [ ] Remover `lucide-react`, `freezegun`, `python-json-logger`
- [ ] `supabase` → `httpx` direto
- [ ] PDF → `fpdf2`, com teste de que acentos e emoji sobrevivem
- [ ] Remover código morto
- [ ] Remover `render.yaml`; ajustar `build-and-push`
- [ ] Renomear `ritmo` → `trevo`
- [ ] Cor default corrigida
- [ ] Versão unificada + primeira tag no CHANGELOG
- [ ] `Makefile lint`
- [ ] `open_finance/README.md`

### Ordem interna
Split de requirements → remoções triviais (`lucide`, `freezegun`, `json-logger`, código morto, configs) → `pydantic` → `passlib` → `python-jose` → `supabase` → PDF. As três últimas são as arriscadas; uma por commit.

### Riscos
- **`passlib` → `bcrypt`**: se o formato divergir, ninguém consegue logar. Teste obrigatório com hash gerado pela versão antiga.
- **`python-jose` → `PyJWT`**: mesma classe de risco para tokens em circulação. Testar decodificação de um token emitido pela versão antiga.
- **PDF**: mudança visual do relatório. Comparar antes/depois manualmente.
- `bcrypt` moderno rejeita senhas acima de 72 bytes em vez de truncar — `validate_password_strength` já valida 72 bytes, então está coberto.

### Validação
```bash
pip install -r requirements.txt   # sem pytest/ruff/bandit
pytest tests/ -q
pip-audit -r requirements.txt --strict
pip-audit -r requirements-lock.txt --strict
cd frontend && npm ci && npm run build
```

### Critérios de aceite
- `pip install -r requirements.txt` não instala pytest, ruff, bandit, factory-boy nem freezegun.
- Um hash bcrypt gerado antes da migração continua verificando (teste).
- Um JWT emitido antes da migração continua decodificando (teste).
- `ecdsa` não aparece mais em `pip freeze`.
- PDF gerado mostra "Alimentação" e "Saúde" com acentos corretos.
- `grep -ri ritmo --include="*.yml" --include="*.yaml" .` não retorna nada.
- Bundle do frontend não regride.

### Estimativa relativa
**L**

### Prompt de implementação
Ver §17 → `PROMPT BP-10`.

---

## BREAKPOINT 11 — CI/CD, observabilidade e operação

### Objetivo
CI-01, CI-02, CI-04, CI-05, CI-06, CI-07, SEC-13, SEC-15.

### Motivação
Um 500 em produção hoje só é descoberto por reclamação. Não há backup testado. A suíte roda três vezes por build.

### Pré-requisitos
BP-08 (passo de migration no deploy já existe).

### Escopo
- **Error tracking:** integrar Sentry (ou equivalente) no backend e no frontend, com `X-Request-Id` como tag e **scrubbing explícito** de e-mail, valores monetários e conteúdo de CSV.
- **Backup:** documentar e **testar** o restore do backup automático do Supabase. Registrar o procedimento e o RTO/RPO reais em `docs/guides/operations.md`. Agendar uma verificação trimestral.
- Substituir o passo de "falhar se pulou" por um hook no `conftest.py` que erra quando `TEST_DATABASE_URL` está definida e um teste é pulado — elimina as duas reexecuções da suíte.
- `concurrency` group no workflow.
- `npm audit --audit-level=high` no frontend.
- `pip-audit` também sobre `requirements-lock.txt`.
- `bandit` sem `-ll` (ou com `-l`), triando os LOW num baseline.
- `build-and-push` só em tag (feito em BP-10; confirmar).
- Uptime check externo apontando para `/api/health`.

### Fora de escopo
APM completo, tracing distribuído, dashboards de métricas. Desproporcional ao porte.

### Arquivos/módulos envolvidos
`.github/workflows/ci.yml` · `tests/conftest.py` · `app/main.py` (init do Sentry) · `frontend/app/layout.tsx` · `requirements.txt` · `docs/guides/operations.md` · `docs/security/incident-response.md`

### Tarefas
- [ ] Sentry no backend com scrubbing de PII e de valores
- [ ] Sentry no frontend, ligado aos error boundaries do BP-07
- [ ] Restore de backup testado e documentado, com RTO/RPO medidos
- [ ] Hook de skip no `conftest.py`; remover as reexecuções do CI
- [ ] `concurrency` group
- [ ] `npm audit` no CI
- [ ] `pip-audit` sobre o lock
- [ ] `bandit` com LOW + baseline
- [ ] Uptime check externo

### Ordem interna
Sentry (maior ganho imediato) → backup (maior risco mitigado) → limpeza de CI.

### Riscos
- **Sentry sem scrubbing vaza dados financeiros para um terceiro.** Configurar `before_send` com denylist explícita e **testar** com um erro proposital que carregue payload sensível antes de habilitar em produção.
- Ligar LOW no bandit gera ruído; criar baseline no mesmo commit.

### Validação
```bash
pytest tests/ -q          # sem TEST_DATABASE_URL → deve avisar, não falhar
TEST_DATABASE_URL=... pytest tests/ -q   # skip deve falhar a suíte
```
Disparar um erro proposital em staging e verificar que chega ao Sentry **sem** e-mail nem valores.

### Critérios de aceite
- Erro em produção aparece no Sentry em menos de 1 minuto, com `request_id`, sem PII.
- Documento de restore existe e registra um restore **executado**, com data e duração.
- CI roda a suíte **uma vez**.
- `npm audit` e `pip-audit` (lock) no pipeline.
- Uptime check ativo com alerta configurado.

### Estimativa relativa
**M**

### Prompt de implementação
Ver §17 → `PROMPT BP-11`.

---

## BREAKPOINT 12 — Open Finance: decisão e fundação

### Objetivo
Transformar o placeholder numa decisão documentada e, se aprovada, numa fundação mínima.

### Motivação
A análise da auditoria anterior está correta: consumir as APIs de Open Finance no Brasil exige autorização do Banco Central, certificados ICP-Brasil e registro no diretório de participantes. O caminho viável é um agregador licenciado (Pluggy, Belvo). **Isso muda a natureza do trabalho de "implementar um protocolo" para "integrar um fornecedor e modelar consentimento, sincronização e expiração".**

### Pré-requisitos
BP-08 (schema estável) e BP-09 (roteadores extraídos). Não faz sentido adicionar um domínio novo ao monólito de 5.000 linhas.

### Escopo (fase 1 — decisão, sem código de integração)
- ADR comparando Pluggy e Belvo: cobertura de instituições, custo por conexão/mês, modelo de consentimento, SLA, requisitos de conformidade repassados ao integrador.
- Modelagem de domínio no papel: `connections` (instituição, status, consentimento, validade), `sync_runs` (lote, janela, resultado), reaproveitamento de `import_batch_id` e `duplicate_hash`.
- Decidir o que acontece na expiração de consentimento (tipicamente 12 meses) e na revogação.
- Decidir a política de reconciliação: transação pendente que vira consolidada, transação que some do extrato.
- **Deixar a UI honesta enquanto não houver integração** — a documentação já é, a interface deve ser.

### Escopo (fase 2 — só se a fase 1 aprovar)
- `app/integrations/<fornecedor>/` implementando o contrato de `base.py`.
- Tabelas `connections` e `sync_runs`, com FK composta `(user_id, id)` como o resto do schema.
- Sincronização sob demanda (não agendada, inicialmente) — evita fila.
- Reuso do pipeline de normalização e deduplicação do CSV.

### Fora de escopo
Qualquer tentativa de falar direto com o Open Finance brasileiro. Sincronização agendada em background. Webhooks.

### Arquivos/módulos envolvidos
`docs/architecture/adr-open-finance.md` (novo) · `docs/product/open-finance.md` · `app/integrations/` · `frontend/` (copy)

### Tarefas
- [ ] ADR com comparação de fornecedores e custo real
- [ ] Modelagem de `connections` e `sync_runs` no ADR
- [ ] Política de consentimento, expiração e revogação escrita
- [ ] Política de reconciliação (pendente → consolidada, transação removida)
- [ ] Revisar a copy do produto para não prometer conexão bancária
- [ ] *(fase 2)* implementação

### Ordem interna
ADR → modelagem → decisão go/no-go → só então código.

### Riscos
- Começar a fase 2 sem a fase 1 produz uma integração amarrada a um fornecedor.
- Custo por conexão pode inviabilizar o produto no estágio atual — por isso o custo entra no ADR antes do código.

### Validação
Revisão do ADR. Nenhuma validação técnica na fase 1.

### Critérios de aceite
- ADR versionado com decisão explícita (incluindo "não fazer agora", se for o caso) e data de reavaliação.
- Nenhuma tela do produto promete conexão bancária que não existe.

### Estimativa relativa
**S** (fase 1) · **XL** (fase 2)

### Prompt de implementação
Ver §17 → `PROMPT BP-12`.

---

# 16. Dependency Graph

```
BP-00 (baseline e testes)
│
├── BP-01 (segurança de sessão e rate limiting)   ──┐
│                                                   │
├── BP-02 (integridade e operações destrutivas)     │
│   └── BP-04 (CSV: correção, robustez, performance)│
│                                                   │
├── BP-03 (domínio financeiro)                      │
│                                                   │
├── BP-06 (performance de banco e bootstrap)        │
│                                                   │
└── BP-07 (frontend) ◄── independente do backend    │
                                                    │
BP-01 ──────────────────────────────────────────────┴── BP-05 (auth e OAuth)

BP-02 ─┐
BP-04 ─┴── BP-08 (schema e migrations)
           └── BP-11 (CI/CD e observabilidade)

BP-00..BP-06 ── BP-09 (extração de roteadores)
                └── BP-10 (dependências e limpeza)
                    └── BP-12 (Open Finance)
```

## O que pode rodar em paralelo

| Trilha | Breakpoints | Observação |
|---|---|---|
| **A — backend crítico** | BP-01 → BP-02 → BP-04 | Sequencial: BP-04 depende do modo destrutivo já corrigido em BP-02. |
| **B — domínio e performance** | BP-03, BP-06 | Independentes entre si e da trilha A. BP-03 exige decisão de produto sobre DOM-05. |
| **C — frontend** | BP-07 | Totalmente independente do backend. Pode começar junto com BP-00. |
| **D — auth** | BP-05 | Depende só de BP-01. |

**Convergência obrigatória:** BP-08 só depois de BP-02 e BP-04 (para agrupar migrations). BP-09 só depois de BP-00 a BP-06 (não mover código que ainda vai ser corrigido). BP-10 só depois de BP-09 (o PDF precisa estar isolado). BP-11 depois de BP-08 (o passo de deploy precisa existir).

**Conflito a evitar:** BP-01 a BP-06 tocam todos o mesmo `app/main.py`. Se rodarem em paralelo, os conflitos de merge serão constantes. **Recomendação: executar as trilhas A e B em série** e deixar só a trilha C (frontend) verdadeiramente paralela.

---

# 17. Implementation Prompts

Cada bloco abaixo é autocontido: pode ser colado numa sessão nova do Claude Code sem mais contexto. Todos compartilham a regra final de escopo.

---

### PROMPT BP-00 — Baseline e proteção contra regressões

```
Você está trabalhando no Trevo: monorepo de finanças pessoais em português.
Backend FastAPI + PostgreSQL em app/ (app/main.py tem 5.133 linhas e concentra
as 56 rotas). Frontend Next.js 15 com output: "export" em frontend/.
Testes em tests/unit e tests/integration; os de integração pulam quando
TEST_DATABASE_URL não está definida.

OBJETIVO
Criar a rede de segurança de testes ANTES de qualquer correção de comportamento.
Hoje nenhum dos defeitos financeiros conhecidos tem teste que o pegue, e o
caminho de autenticação de produção (cookie HttpOnly + header X-CSRF-Token)
nunca é exercitado — todos os testes usam Authorization: Bearer, que é
explicitamente isento do middleware CSRF em app/main.py (CSRF_EXEMPT_PATHS e a
checagem de has_bearer).

ESCOPO
1. .gitignore: hoje ignora *.csv e *.pdf, o que impede versionar fixtures dos
   dois recursos centrais do produto. Adicionar negação explícita para
   tests/fixtures/** com comentário explicando o porquê.
2. scripts/check-ignored-files.sh: ampliar as extensões verificadas para incluir
   csv, pdf, json, mjs, mts, yml. Corrigir o DIRS=("${@:-...}") atual, que cria
   um único elemento de array e só funciona por causa do word-splitting.
3. Criar tests/fixtures/csv/ com 9 fixtures SINTÉTICAS (não use dados reais):
   - pt_br.csv          valores "1.234,56", delimitador ;
   - en_us.csv          valores "1,234.56", delimitador ,
   - parenteses.csv     negativos como "(123,45)"
   - preambulo.csv      4 linhas de cabeçalho de banco antes da linha de colunas
   - com_hora.csv       coluna de hora separada
   - duplicatas.csv     duas linhas legítimas idênticas em data/descrição/valor
   - tab.csv            delimitador \t
   - utf8_bom.csv       UTF-8 com BOM
   - cp1252.csv         Windows-1252 com aspas curvas nas descrições
4. tests/conftest.py: criar a fixture `cookie_client` que autentica pelo caminho
   real — POST /api/auth/login (form-urlencoded), guardar os cookies,
   GET /api/auth/csrf, e enviar X-CSRF-Token nos métodos mutantes.
5. Replicar ao menos 5 testes de integração existentes usando cookie_client.
6. Adicionar um teste marcado @pytest.mark.xfail(strict=True) para CADA defeito
   abaixo, com reason citando o ID. Eles devem falhar hoje e passar depois:
   - DATA-01: importar CSV em modo "replace" no mês M apaga um lançamento MANUAL
     preexistente em M (não deveria)
   - DOM-01: criar duas vezes a mesma compra parcelada (mesmo título, cartão e
     data) gera installment_group idêntico (deveriam ser distintos)
   - DOM-02: interestRate=0.4 produz juros zero (deveria produzir juros > 0)
   - DOM-03: POST /api/transactions com cardId e data após o closing_day grava
     billing_month do mês da compra (deveria ser o mês seguinte)
   - DOM-04: com o relógio em 2026-09-30 23:30 America/Sao_Paulo,
     get_current_month() devolve "2026-10" (deveria devolver "2026-09")
   - SEC-03: POST /api/categories com nome de categoria ativa existente muda o
     type dela (deveria responder 409)
   - CSV-01: parse_decimal_text("1,234.56") devolve 1.23456 (deveria ser 1234.56)
   - CSV-02: parse_decimal_text("(123,45)") devolve positivo (deveria ser negativo)
   - CSV-04: importar preambulo.csv falha (deveria funcionar)
   - FIN-01: duas transações legítimas idênticas no mesmo dia, uma é descartada
7. Criar pyproject.toml com [tool.pytest.ini_options] (testpaths, asyncio_mode,
   markers). Não existe nenhuma config de pytest hoje.
8. .github/workflows/ci.yml: adicionar `npm run test:money` ao passo de frontend
   (o script existe em frontend/package.json e nunca roda no CI).

RESTRIÇÕES
- NÃO corrija nenhum dos defeitos. Este breakpoint só adiciona testes.
- NÃO altere app/main.py, app/shared/, app/integrations/ nem frontend/.
  As únicas exceções são tests/, scripts/, .gitignore, pyproject.toml e o CI.
- Fixtures devem ser 100% sintéticas — nada de dados financeiros reais.

VALIDAÇÃO
  bash scripts/check-ignored-files.sh
  pytest tests/ -q -rs --strict-markers
  cd frontend && npm run test:money

CRITÉRIOS DE ACEITE
- check-ignored-files.sh detecta um .csv colocado propositalmente fora de
  tests/fixtures/ e falha.
- As 9 fixtures estão versionadas (git ls-files tests/fixtures/ lista as 9).
- Ao menos 5 testes de integração passam pelo caminho cookie+CSRF.
- Existe um xfail(strict=True) para cada um dos 10 IDs acima, e todos estão
  xfailing (não xpassing).
- pytest -q não reporta nenhum erro de coleta.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados. Se encontrar outro defeito durante o
trabalho, anote-o no corpo do PR em vez de corrigi-lo.
```

---

### PROMPT BP-01 — Segurança de sessão e rate limiting

```
Projeto Trevo (FastAPI + Next.js 15 static export + PostgreSQL). Backend em
app/main.py; cliente HTTP do frontend em frontend/lib/api.ts.

CONTEXTO DOS DOIS DEFEITOS

SEC-01 — o cache de respostas do frontend vaza dados entre usuários.
frontend/lib/api.ts mantém `const responseCache = new Map()` de módulo, com
chave `${token || "public"}::${path}`. O `token` passado por toda a aplicação é a
CONSTANTE COOKIE_AUTH_TOKEN = "__trevo_cookie_session__" (a sessão real está no
cookie HttpOnly), então a chave é idêntica para qualquer usuário. O logout em
frontend/app/configuracoes/page.tsx chama clearSession() (que mexe só em
localStorage/sessionStorage) e router.replace("/login") — navegação client-side,
que NÃO recarrega o módulo. Resultado: usuário A sai, B entra na mesma aba, e
por até 30 s (GET_CACHE_TTL_MS) B vê o painel financeiro de A.

SEC-02 — o rate limit por IP não existe em produção.
app/main.py usa `Limiter(key_func=get_remote_address)` do slowapi sem
storage_uri, o que resolve para MemoryStorage (verificado em runtime). Em
serverless cada instância tem o próprio contador. As rotas com @limiter.limit
(register 3/h, change-password 3/h, delete account 5/h, privacy/export 10/h,
export/csv 20/h) estão efetivamente sem limite. A camada de login por e-mail,
persistida em login_failures_state, funciona e deve ser o modelo a seguir.

ESCOPO
1. frontend/lib/api.ts: exportar `clearApiCache()` que limpa responseCache e
   pendingRequests. Chamá-la em clearSession() (frontend/lib/authSession.ts) e
   após login e registro bem-sucedidos (frontend/app/login/page.tsx e
   frontend/app/cadastro/page.tsx). Adicionalmente, passar a incluir o id do
   usuário autenticado na chave do cache quando ele já for conhecido.
2. Nova migration criando rate_limit_state(key_hash TEXT PRIMARY KEY,
   window_start TIMESTAMPTZ NOT NULL, count INTEGER NOT NULL DEFAULT 0).
   Siga o padrão de migrations/0005_security_hardening.sql.
3. Substituir o limiter em memória por checagem persistida em Postgres,
   espelhando enforce_login_rate_limit/record_login_failure. Deve DEGRADAR COM
   GRAÇA: se o banco falhar, logar e PERMITIR o request (fail-open) — nunca
   bloquear login por indisponibilidade de banco.
4. Adicionar rate limit a POST /api/imports/csv/upload (hoje sem nenhum; grava
   até 1 MB de JSONB por chamada).
5. Montar TrustedHostMiddleware com allowlist derivada de nova variável
   TRUSTED_HOSTS (separada por vírgula) mais os hosts de ALLOWED_ORIGINS. Em
   desenvolvimento, permitir localhost/127.0.0.1. Documentar em .env.example.
   Motivo: app/oauth.py deriva o redirect do frontend de request.base_url, que
   vem do header Host — hoje sem validação (open redirect).

RESTRIÇÕES
- NÃO mexa em OAuth além do TrustedHostMiddleware (isso é o BP-05).
- NÃO altere ACCESS_TOKEN_EXPIRE_HOURS (BP-05).
- NÃO refatore app/main.py estruturalmente (BP-09).
- O limiter novo não pode adicionar query em rotas que hoje não são limitadas.

ARQUIVOS
frontend/lib/api.ts, frontend/lib/authSession.ts, frontend/app/login/page.tsx,
frontend/app/cadastro/page.tsx, frontend/app/configuracoes/page.tsx,
app/main.py, app/core/config.py, migrations/, .env.example, tests/

TESTES
- O xfail de SEC-01 (BP-00) deve passar: após clearSession(), responseCache vazio.
- Novo teste: exceder o limite de cadastro, simular reinício de processo
  (limpar estruturas em memória) e confirmar que o limite continua valendo.
- Novo teste: request com Host não listado recebe 400; Host de produção passa.

CRITÉRIOS DE ACEITE
- clearApiCache existe, é chamada em logout/login/registro, e há teste.
- rate_limit_state criada por migration e usada pelas rotas limitadas.
- Banco indisponível NÃO bloqueia login (teste).
- TrustedHostMiddleware montado, com teste nos dois sentidos.
- pytest tests/ -q e (cd frontend && npx tsc --noEmit && npm run lint) limpos.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados.
```

---

### PROMPT BP-02 — Integridade de dados e operações destrutivas

```
Projeto Trevo (FastAPI + PostgreSQL + Next.js). Quatro operações do produto
podem destruir ou corromper histórico financeiro sem recuperação.

DEFEITO 1 (DATA-01) — app/main.py, confirm_csv_import
O modo "replace" executa:
  DELETE FROM transactions
  WHERE user_id = %s
    AND COALESCE(billing_month, substring(transaction_date from 1 for 7)) = ANY(%s)
Sem filtro por `source`. Importar o extrato de setembro apaga o aluguel digitado
à mão, a parcela 4/12 da geladeira e a mensalidade recorrente. Pior: apagar uma
parcela do meio deixa o installment_group com um buraco permanente — a compra
passa a aparecer como "12 parcelas, 11 restantes" para sempre em
get_grouped_installment_purchases (que usa MAX(total_installments) e COUNT(*)).

DEFEITO 2 (DOM-01) — app/main.py, create_installments e
create_installments_without_card
  group = f"{user_id}-{card_id}-{title}-{purchase_date}"
Chave natural derivada de texto do usuário. Duas compras iguais no mesmo dia
colidem: installment_number duplica, createdInstallments devolve 24 em vez de 12
(porque relê o grupo inteiro), e delete_transaction apaga as duas compras.

DEFEITO 3 (SEC-03) — app/main.py, create_category
  ON CONFLICT (user_id, name) DO UPDATE SET type = EXCLUDED.type, ...
Criar "Mercado" como income quando já existe "Mercado" como expense muda o type
da categoria existente e reclassifica todo o histórico ligado a ela.

DEFEITO 4 (FIN-10) — app/main.py, delete_transaction
Apagar uma parcela apaga o grupo inteiro sem sinalização prévia; a API só avisa
depois, com deletedGroup: true.

ESCOPO
1. Migration: ALTER TABLE transactions ADD COLUMN IF NOT EXISTS import_batch_id
   UUID; índice (user_id, import_batch_id). Seguir o estilo de
   migrations/0012_transaction_month_index.sql (comentário explicando o porquê).
2. confirm_csv_import grava o mesmo import_batch_id em todas as linhas do lote.
3. Substituir o modo "replace" por "desfazer última importação": apagar apenas
   as linhas do import_batch_id anterior do usuário. Se preferir preservar o
   modo por mês, restringi-lo a `source = 'csv_import' AND installment_group IS
   NULL` — mas a opção recomendada é o undo por lote.
4. installment_group passa a ser str(uuid4()) nos DOIS pontos de criação.
5. Criar scripts/check-installment-groups.sql — SOMENTE LEITURA — que lista
   grupos com installment_number duplicado (colisões já ocorridas). Não corrija
   dados automaticamente.
6. create_category: responder 409 quando existir categoria ATIVA com o mesmo
   nome. O upsert passa a servir só para reativar categoria arquivada
   (is_active = FALSE), e NUNCA altera `type`.
7. delete_transaction: exigir query param scope=group para apagar o grupo. Sem
   ele, responder 409 com a contagem de parcelas afetadas. Atualizar
   frontend/lib/api.ts e a tela de transações para confirmar com o usuário.
8. Reescrever a copy de frontend/components/ImportConfirmDialog.tsx para dizer
   exatamente o que será apagado.

RESTRIÇÕES
- NÃO altere o parsing de CSV, o duplicate_hash nem a performance da importação
  (tudo isso é o BP-04).
- NÃO escreva migration que apague ou altere dados existentes. Apenas DDL.
- NÃO corrija colisões de installment_group já existentes — só relate.

ARQUIVOS
app/main.py (confirm_csv_import, create_installments,
create_installments_without_card, create_category, delete_transaction),
migrations/, scripts/check-installment-groups.sql,
frontend/components/ImportConfirmDialog.tsx, frontend/app/transacoes/page.tsx,
frontend/lib/api.ts, tests/

TESTES
Os xfail de DATA-01, DOM-01 e SEC-03 (criados no BP-00) devem passar. Somar:
- importar CSV do mês M preserva lançamento manual preexistente em M
- importar CSV do mês M preserva parcela de cartão com billing_month = M
- duas compras parceladas idênticas geram dois installment_group distintos
- POST /api/categories com nome de categoria ativa → 409, type inalterado
- POST /api/categories com nome de categoria arquivada → reativa, type inalterado
- DELETE de parcela sem scope=group → 409 com contagem

CRITÉRIOS DE ACEITE
- Os seis testes acima passam.
- scripts/check-installment-groups.sql roda e não contém DELETE/UPDATE.
- Nenhum xfail virou xpass inesperado.
- pytest tests/ -q e (cd frontend && npx tsc --noEmit) limpos.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados.
```

---

### PROMPT BP-03 — Correções de domínio financeiro

```
Projeto Trevo (FastAPI + PostgreSQL). Público pt-BR. Seis defeitos fazem os
números centrais do produto ficarem errados.

ATENÇÃO — DECISÃO DE PRODUTO NECESSÁRIA ANTES DE CODAR (DOM-05)
Três fórmulas independentes somam settings.monthly_income (renda configurada) a
`inflow` (soma de TODAS as transações de tipo income do mês):
  get_dashboard:   balance = base_income + inflow - outflow
  _compute_goals:  available_budget = monthly_income + inflow - reserve_amount
  calculate_score: denominator = monthly_income + inflow
Se o usuário lançar o salário como transação de entrada — o gesto natural — a
renda é contada DUAS VEZES: orçamento dobra, meta diária dobra, score infla,
alertas param de disparar. Nada impede nem avisa.
Pergunte ao usuário qual semântica ele quer antes de implementar. Recomendação:
monthly_income vira RENDA ESPERADA e a renda efetiva passa a ser
max(monthly_income, inflow), calculada num único helper. Documente a decisão
escolhida num comentário no helper.

DEFEITO DOM-04 — tudo em UTC
app/shared/dates.py::get_current_month usa datetime.now(UTC), e
app/main.py::_compute_goals usa datetime.now(UTC).date(). Brasília é UTC-3:
das 21h à meia-noite o servidor já está no dia seguinte. progress_day avança
cedo (distorcendo currentAverageSpend, projectedClosing e availableToday), e no
dia 30 às 21h30 get_current_month() já devolve o mês seguinte.

DEFEITO DOM-03 — compra avulsa no cartão ignora o fechamento
app/main.py::create_transaction insere billing_month como veio no payload e
nunca chama first_billing_month (app/shared/dates.py), apesar de aceitar cardId.
create_installments já chama. Compra no crédito em 25/09 com closing_day 20 cai
na fatura de setembro; deveria cair na de outubro.

DEFEITO DOM-02 — taxa de juros arredondada para centavos
app/main.py, simulate_installments e create_installments_without_card:
  interest_rate = round_money(Decimal(payload.interestRate) / Decimal("100"))
round_money quantiza para 0.01 — mas isso é uma TAXA, não dinheiro. 1,99% vira
2%; 0,4% vira 0% e os juros somem. Além disso Decimal(float) contraria o
to_decimal(str(...)) usado em todo o resto do código. As duas funções têm o
bloco duplicado literalmente.

DEFEITO FIN-02 — calendário diário discorda do total
Em _compute_goals, a série por dia filtra
  transaction_date BETWEEN start AND end AND (billing_month IS NULL OR billing_month = month)
enquanto outflow_to_today filtra pelo mês efetivo (COALESCE). Parcelas compradas
em meses anteriores entram no total e não nas barras. As barras não somam o total.

DEFEITO FIN-09 — edição livre de linha de parcelamento
update_transaction permite trocar type, billing_month e amount de uma linha com
installment_group, quebrando a integridade do grupo. Também não recalcula
duplicate_hash ao editar um lançamento importado, deixando hash obsoleto.

ESCOPO
1. Criar app/shared/clock.py com today(), now() e current_month() num fuso
   configurável (APP_TIMEZONE em app/core/config.py, default "America/Sao_Paulo",
   via zoneinfo). Substituir TODOS os usos de "agora" de DECISÃO DE NEGÓCIO.
   Timestamps de persistência (created_at, audit_log, revoked_tokens) continuam
   em UTC — não os toque.
2. create_transaction: quando cardId vier e billingMonth não vier explícito,
   calcular billing_month com first_billing_month(transaction_date, closing_day).
3. Extrair apply_installment_interest() para app/shared/money.py, sem
   round_money na taxa e usando to_decimal(str(rate)). Usar nas duas funções.
4. Implementar a decisão de DOM-05 num helper único usado pelas três fórmulas.
5. Alinhar o filtro da série diária ao filtro do total.
6. update_transaction: 409 ao tentar alterar type/billing_month/amount de linha
   com installment_group; recalcular duplicate_hash quando source='csv_import'.

RESTRIÇÕES
- NÃO introduza transferência, estorno nem conta bancária (fora de escopo).
- NÃO altere o schema (nenhuma migration neste breakpoint).
- NÃO refatore app/main.py estruturalmente (BP-09).
- Timestamps de persistência permanecem em UTC.

ARQUIVOS
app/shared/clock.py (novo), app/shared/dates.py, app/shared/money.py,
app/core/config.py, app/main.py (create_transaction, update_transaction,
_compute_goals, get_dashboard, calculate_score, simulate_installments,
create_installments_without_card), .env.example, tests/

TESTES
Os xfail de DOM-02, DOM-03, DOM-04 e DOM-05 (BP-00) devem passar. Somar:
- com o relógio em 2026-09-30 23:30 BRT, current_month() == "2026-09"
- compra avulsa no crédito após o fechamento cai no mês seguinte
- compra parcelada após o fechamento continua correta (não regrediu)
- interestRate=0.4 produz juros > 0; interestRate=1.99 não vira 2.00
- soma das barras diárias == outflowToToday
- alterar type de linha de parcelamento → 409

CRITÉRIOS DE ACEITE
- Todos os testes acima passam.
- `grep -rn "datetime.now(UTC)" app/` só retorna ocorrências de persistência,
  auditoria ou tokens (verificado linha a linha no PR).
- A decisão de DOM-05 está documentada em comentário no helper.
- pytest tests/ -q e ruff check . limpos.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados.
```

---

### PROMPT BP-04 — CSV: correção, robustez e performance

```
Projeto Trevo. A importação de extrato em CSV é o recurso mais diferenciado do
produto e tem os defeitos mais concretos. Fixtures já existem em
tests/fixtures/csv/ (criadas no BP-00).

DEFEITOS DE PARSING (app/integrations/normalizer.py::parse_decimal_text)
- CSV-01: quando "," e "." aparecem juntos, o código assume SEMPRE formato pt-BR.
  "1,234.56" vira "1,23456" vira 1.23456 — mil vezes menor. A regra correta é:
  o separador que aparece POR ÚLTIMO é o decimal.
- CSV-02: "(123,45)" (negativo entre parênteses) perde os parênteses no regex e
  vira positivo. Sem coluna de tipo, parse_import_type classifica como receita.
- CSV-03: "1.234,56-" (sinal posposto) faz Decimal() levantar.

DEFEITOS DE LEITURA (app/main.py::parse_csv_rows, detect_csv_delimiter)
- CSV-04: csv.DictReader lê a linha 1 como cabeçalho. Extratos de Itaú, Bradesco
  e Santander têm linhas de preâmbulo antes. Esses arquivos falham.
- CSV-05: só detecta ";" e ",". Tab e pipe não.
- CSV-06: fallback de encoding é latin-1, que nunca levanta — arquivos cp1252
  decodificam com caracteres de controle nas descrições.
- CSV-07: csv_safe_cell prefixa "'" em células que começam com =+-@ na
  EXPORTAÇÃO (proteção correta contra injeção de fórmula), mas a leitura não
  desfaz — o apóstrofo volta na reimportação.

PERFORMANCE (PERF-01)
app/main.py::resolve_import_category é chamada UMA VEZ POR LINHA e faz duas
idas ao banco (list_categories + match_categorization_rule), nenhuma delas com
request_cached. Com CSV_IMPORT_MAX_ROWS = 5000 são até 10.000 queries — e em
serverless cada db_cursor() abre uma CONEXÃO NOVA (app/core/database.py).
confirm_csv_import ainda chama build_csv_import_preview de novo e depois faz
mais um SELECT de duplicata por linha. Extratos de 1.500+ linhas não completam
dentro do limite de execução da função.

DEDUPLICAÇÃO (FIN-01)
build_duplicate_hash(user_id, date, description, amount, type) não inclui hora
nem conta. Com o índice ÚNICO idx_transactions_user_duplicate_hash, duas
passagens de ônibus de R$ 6,00 no mesmo dia são impossíveis de importar juntas.
O código já mantém um "legacy_duplicate_hash" para compatibilidade — preserve
esse mecanismo ao mudar a fórmula, ou transações já importadas serão duplicadas.

COLUNAS (CSV-08, CSV-09)
payment_method = row.get("account") or "csv_import" mistura conta de origem com
forma de pagamento e polui o paymentMethodBreakdown do dashboard.
external_id recebe o duplicate_hash, inutilizando o campo para Open Finance.

ESCOPO
1. parse_decimal_text: regra do último separador; parênteses como negativo;
   sinal posposto.
2. parse_csv_rows: detectar a linha de cabeçalho (primeira linha cujas células em
   maioria não são numéricas e cujo número de colunas se repete nas seguintes);
   usar csv.Sniffer para o delimitador com fallback à heurística atual; tentar
   utf-8-sig → cp1252 → latin-1.
3. Permitir que o preview informe/ajuste a linha de cabeçalho escolhida
   (campo opcional no payload + UI em frontend/app/importar/page.tsx).
4. Carregar categorias e regras UMA VEZ por importação (dicionário em memória
   passado para resolve_import_category).
5. Substituir o SELECT de duplicata por linha por INSERT ... ON CONFLICT
   (user_id, duplicate_hash) DO NOTHING em lote (psycopg2.extras.execute_values),
   usando o índice único que já existe.
6. confirm_csv_import reusa o resultado do preview guardado na sessão em vez de
   recomputar.
7. duplicate_hash passa a incluir hora e conta quando existirem. MANTENHA a
   consulta pelo hash legado.
8. Migration: transactions.account TEXT. payment_method volta a receber só forma
   de pagamento. external_id deixa de receber o hash.
9. Desfazer o prefixo de csv_safe_cell na leitura.

RESTRIÇÕES
- NÃO altere o modo destrutivo da importação (já corrigido no BP-02).
- NÃO implemente perfis de banco salvos (mapeamento persistido por instituição).
- NÃO implemente OFX nem Open Finance.
- NÃO invalide deduplicação histórica: o hash legado deve continuar consultado.

ARQUIVOS
app/integrations/normalizer.py, app/main.py (parse_csv_rows,
detect_csv_delimiter, resolve_import_category, build_csv_import_preview,
upload_csv_import, confirm_csv_import), migrations/,
frontend/app/importar/page.tsx, tests/integration/test_csv_import.py

TESTES
Os xfail de CSV-01, CSV-02, CSV-04 e FIN-01 (BP-00) devem passar. Somar:
- um teste por fixture de tests/fixtures/csv/, conferindo valor, sinal e data
- reimportar um arquivo já importado não cria duplicatas (hash legado funciona)
- preview de 3.000 linhas faz menos de 10 queries (instrumentar o cursor) e
  completa em menos de 5 s localmente

CRITÉRIOS DE ACEITE
- As 9 fixtures importam corretamente.
- parse_decimal_text("1,234.56") == 1234.56 e ("1.234,56") == 1234.56.
- parse_decimal_text("(123,45)") é negativo.
- preambulo.csv é lido sem intervenção manual.
- Contagem de queries do preview de 3.000 linhas < 10.
- Duas transações legítimas com horas diferentes no mesmo dia são ambas
  importadas.
- pytest tests/ -q limpo.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados.
```

---

### PROMPT BP-05 — Superfície de autenticação e OAuth

```
Projeto Trevo (FastAPI). Autenticação por JWT em cookie HttpOnly, com suporte a
Bearer; OAuth de Google, GitHub e Facebook em app/oauth.py; LGPD em
app/privacy/service.py.

DEFEITO SEC-04 — vinculação OAuth silenciosa
app/main.py::resolve_oauth_user: se não encontra a identidade social mas encontra
um usuário PELO E-MAIL, faz UPDATE users SET auth_provider, oauth_subject e
devolve o usuário — login concluído. Sem confirmação de senha, sem verificação,
sem aviso. A segurança da conta financeira passa a depender inteiramente da
política de verificação de e-mail de terceiros. Hoje os três provedores
verificam, então o risco imediato é baixo; o problema é a ausência de defesa em
profundidade e o fato de um quarto provedor sem essa checagem virar takeover.

DEFEITO SEC-05 — segredo de assinatura em texto claro no banco
app/core/signing.py: sem JWT_SECRET_KEY no ambiente, o servidor gera
secrets.token_urlsafe(48) e grava em app_secrets.value como TEXT. Quem lê essa
linha (dump, snapshot, painel do Supabase, service-role key vazada, SELECT
somente-leitura) pode assinar token para qualquer user_id. Não há rotação.

DEFEITO SEC-07 — sessão de 168 horas sem refresh nem timeout de inatividade.

DEFEITOS DE CONFORMIDADE
- SEC-08: build_data_export descobre tabelas por user_id, pula as terminadas em
  "_state" e as de _SKIP_TABLES = {"revoked_tokens"}. card_pins não casa nenhum
  filtro, então pin_hash vai para a exportação do titular — contradizendo o
  princípio declarado no próprio módulo ("never the password hash").
- SEC-09: update_consent com scope="terms_privacy" e granted=false grava a
  revogação no ledger e a conta continua plenamente ativa. É um no-op.
- SEC-11: storage.remove_avatar engole exceções. Se o Supabase falhar na
  exclusão de conta, o objeto fica no bucket e a eliminação LGPD fica incompleta
  sem registro.
- SEC-12: login e register devolvem access_token no corpo JSON além de setar o
  cookie; o SPA descarta.

ESCOPO
1. resolve_oauth_user deixa de vincular automaticamente quando o usuário
   encontrado por e-mail tem hashed_password e não tem oauth_subject. Responder
   com erro orientando a entrar com senha e vincular pelo perfil. Criar rota
   AUTENTICADA POST /api/auth/oauth/{provider}/link + UI em
   frontend/app/perfil/page.tsx.
2. app/core/signing.py: logar WARNING em produção quando o segredo vier de
   app_secrets; expor o estado (origem do segredo) em GET /api/health.
   Documentar o procedimento de rotação em docs/security/security.md.
3. ACCESS_TOKEN_EXPIRE_HOURS default para 24. Implementar renovação deslizante:
   reemitir o cookie em requests autenticados quando faltar menos de 25% da
   validade.
4. Adicionar card_pins a _SKIP_TABLES (ou exportar a tabela sem pin_hash).
5. Revogação de terms_privacy passa a ter efeito: iniciar o fluxo de exclusão
   (com dupla confirmação) ou desativar a conta informando o prazo. Escolha uma
   e documente.
6. Registrar falhas de remoção de avatar numa tabela de pendências de
   eliminação, com procedimento manual em docs/guides/operations.md.
7. Parar de devolver access_token no corpo para o SPA; manter para clientes que
   peçam explicitamente (header Accept ou parâmetro).

RESTRIÇÕES
- NÃO implemente MFA/TOTP (fora de escopo).
- NÃO permita múltiplos provedores por usuário (fora de escopo).
- NÃO altere o middleware CSRF nem o TrustedHostMiddleware (já feitos no BP-01).
- Antes de bloquear a vinculação automática, verifique quantos usuários atuais
  entram por social numa conta criada com senha, e relate o número.

ARQUIVOS
app/main.py (resolve_oauth_user, oauth_callback, login, register, update_consent,
delete_account, health, get_current_user), app/oauth.py, app/core/signing.py,
app/core/config.py, app/privacy/service.py, migrations/,
frontend/app/perfil/page.tsx, frontend/components/auth/SocialLoginButtons.tsx,
docs/security/security.md, docs/guides/operations.md, tests/

TESTES
- Callback OAuth com e-mail de conta existente COM senha não autentica
- Vinculação pelo perfil, autenticada, funciona
- GET /api/privacy/export não contém "pin_hash"
- Token emitido há 20 h é renovado; token de 25 h é rejeitado
- /api/health informa a origem do segredo de assinatura

CRITÉRIOS DE ACEITE
- Os cinco testes acima passam.
- JWT_SECRET_KEY cadastrada no ambiente de produção (ação operacional; registrar
  no PR que foi feita).
- Revogação de terms_privacy produz efeito observável e testado.
- pytest tests/ -q e ruff check . limpos.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados.
```

---

### PROMPT BP-06 — Performance de banco e do bootstrap

```
Projeto Trevo (FastAPI + PostgreSQL, deploy serverless na Vercel).
GET /api/bootstrap é o endpoint que toda tela chama.

DEFEITOS
PERF-02 — app/main.py::get_cards_summary é N+1 em duas dimensões: para cada
cartão faz 1 query de fatura + 1 de grupos de parcelas + 1 query POR GRUPO, e
ainda chama get_card_commitment, que abre a PRÓPRIA conexão aninhada. Com 3
cartões e 20 grupos: ~70 queries.

PERF-03 — bootstrap() chama get_cards_summary diretamente E dentro de
get_dashboard: ela roda DUAS vezes. calculate_score roda duas vezes (mês atual e
anterior), cada uma com laço de get_invoice_total por cartão.
get_alerts_for_month tem o mesmo laço. request_cached (ContextVar, escopo de
request) hoje cobre apenas goals e budget_summary.

PERF-04 — app/core/database.py: em serverless, connection() faz
psycopg2.connect() a CADA chamada. Há 79 usos de db_cursor em main.py; um
bootstrap abre dezenas de conexões TCP+TLS.

PERF-07 — simulate_card_invoices faz uma query por mês num laço (até 24).

ESCOPO
1. Criar um middleware de debug, atrás da env var TREVO_DEBUG_QUERIES, que conta
   queries e conexões por request e loga o total. Use-o para medir ANTES.
2. Consolidar get_cards_summary em 2 queries totais, independentemente do número
   de cartões: uma agregação de fatura agrupada por card_id, uma agregação de
   parcelas agrupada por (card_id, installment_group). Montar o resultado em
   Python. Incorporar get_card_commitment na mesma agregação.
3. Estender request_cached a get_settings, list_categories, list_cards,
   get_cards_summary, get_invoice_total e calculate_score.
4. Consolidar os laços por cartão de calculate_score e get_alerts_for_month numa
   agregação única agrupada por card_id.
5. simulate_card_invoices: uma query com GROUP BY em vez de uma por mês.
6. Em serverless, manter UMA conexão por request: guardá-la num ContextVar,
   abri-la sob demanda no primeiro db_cursor() e fechá-la no fim do request (no
   middleware add_request_id, que já gerencia o _request_cache).
7. Medir DEPOIS e registrar antes/depois na descrição do PR.

RESTRIÇÕES
- NÃO mude o formato de nenhuma resposta da API. Este breakpoint é puramente de
  performance; os payloads devem ser byte-a-byte equivalentes.
- NÃO migre transaction_date de TEXT para DATE (fora de escopo).
- NÃO introduza cache entre requests, Redis nem qualquer serviço externo.
- ATENÇÃO à semântica transacional: hoje cada db_cursor(commit=True) é uma
  transação isolada. Com conexão compartilhada por request, um rollback pode
  afetar o que veio antes. Mantenha commit explícito e teste TODAS as rotas de
  escrita.
- ATENÇÃO ao request_cached em get_cards_summary: verifique que nenhum endpoint
  escreve e relê cartões no mesmo request antes de cachear.

ARQUIVOS
app/core/database.py, app/main.py (get_cards_summary, get_card_commitment,
calculate_score, get_alerts_for_month, simulate_card_invoices, bootstrap,
request_cached, add_request_id), tests/

TESTES
- Toda a suíte de integração continua verde (nenhuma resposta mudou).
- Novo teste: /api/bootstrap com 3 cartões e 20 grupos de parcelas faz menos de
  20 queries (asserção via contador do middleware de debug).
- Novo teste: um request abre exatamente 1 conexão em modo serverless.
- Novo teste: uma rota de escrita que falha no meio faz rollback sem afetar
  escritas anteriores do mesmo request (se houver esse caso).

CRITÉRIOS DE ACEITE
- /api/bootstrap com 3 cartões e 20 grupos: < 20 queries (hoje ~150).
- 1 conexão por request em serverless.
- Suíte de integração completa verde.
- Números antes/depois na descrição do PR.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados.
```

---

### PROMPT BP-07 — Frontend: boundaries, bundle e acessibilidade

```
Projeto Trevo. Frontend Next.js 15 App Router com output: "export", Tailwind com
tokens CSS em frontend/app/globals.css, tema claro/escuro via
frontend/lib/theme.tsx. Este breakpoint NÃO toca o backend.

DEFEITOS
UX-01 — não existe app/error.tsx, app/global-error.tsx nem app/not-found.tsx.
Um erro de render mostra a tela padrão do Next, sem identidade nem recuperação.

PERF-05 — medido com `next build`: /relatorios tem 121 kB de página e 240 kB de
First Load JS, o dobro de qualquer outra rota, porque
frontend/app/relatorios/page.tsx importa recharts estaticamente (linha 5). O
padrão para resolver isso JÁ EXISTE e funciona em frontend/components/
SummaryHome.tsx, que usa next/dynamic com ssr: false e um ChartSkeleton de
altura exata. O comentário em ChartSkeleton.tsx explica por que a altura precisa
bater (evitar CLS) e por que os blocos inteiros precisam ser extraídos (recharts
inspeciona seus filhos; wrappers dinâmicos quebram a introspecção).

UX-02 — frontend/components/ImportConfirmDialog.tsx tem o padrão de diálogo
acessível completo: role="dialog", aria-modal, aria-labelledby/describedby,
focus trap com Tab/Shift+Tab, Escape e restauração de foco.
CreateCategoryDrawer.tsx e FinancialPlanningDrawer.tsx não têm NENHUM desses —
só um botão com aria-label="Fechar".

UX-03 — após POST /api/auth/change-password o backend grava password_changed_at
e passa a rejeitar o token atual (iat < password_changed_at). Correto em
segurança, mas o usuário é jogado para /login sem explicação.

UX-04 — não há manifest PWA nem theme-color, num produto que o README descreve
como mobile-first.

PERF-06 — frontend/app/layout.tsx declara Inter-latin.woff2 e
Inter-latin-ext.woff2 como duas entradas de src SEM unicodeRange (idem Nunito).
Sem unicode-range o subsetting não tem efeito. Para pt-BR, latin-ext
provavelmente é desnecessária.

PERF-08 — downloadFile em frontend/app/relatorios/page.tsx cria um objectURL e
nunca chama URL.revokeObjectURL.

OUTROS
- O handle de resize da sidebar em AppShell.tsx responde a ArrowLeft/ArrowRight
  mas não se anuncia: falta role="separator", aria-orientation, aria-valuenow/
  min/max.
- As regiões de mensagem (setMessage inline nas páginas) não usam aria-live.
- frontend/app/importar/page.tsx usa console.error em .catch() de handlers de UI,
  engolindo o erro em vez de transformá-lo em estado visível.

ESCOPO
1. Criar app/error.tsx, app/global-error.tsx e app/not-found.tsx com a identidade
   Trevo (usar os tokens de globals.css) e um caminho de recuperação claro.
2. Extrair o gráfico de barras de /relatorios para
   components/charts/ReportBarChart.tsx e carregá-lo com next/dynamic + um
   ChartSkeleton de altura exata, replicando o padrão de SummaryHome.tsx.
3. Extrair o padrão de diálogo de ImportConfirmDialog para
   components/Dialog.tsx (props: open, onClose, titleId, describedById, busy) e
   aplicá-lo aos TRÊS diálogos. Verifique manualmente com teclado que o
   ImportConfirmDialog não regrediu.
4. ARIA no handle de resize da sidebar.
5. URL.revokeObjectURL após o download.
6. Definir unicodeRange nas faces, ou remover latin-ext se não for necessária —
   meça o impacto no bundle e registre a decisão.
7. Criar public/manifest.webmanifest com ícones maskable e theme-color; ligar no
   metadata de app/layout.tsx.
8. Após troca de senha, mostrar mensagem explícita antes de redirecionar.
9. aria-live="polite" nas regiões de mensagem; substituir console.error por
   estado de erro visível em importar/page.tsx.

RESTRIÇÕES
- NÃO altere nada em app/ (backend Python).
- NÃO faça redesign nem introduza componentes de produto novos.
- NÃO adicione service worker nem funcionalidade offline.
- NÃO troque recharts por outra biblioteca.

ARQUIVOS
frontend/app/error.tsx, global-error.tsx, not-found.tsx (novos),
frontend/app/relatorios/page.tsx, frontend/components/charts/ReportBarChart.tsx
(novo), frontend/components/Dialog.tsx (novo),
frontend/components/ImportConfirmDialog.tsx, CreateCategoryDrawer.tsx,
FinancialPlanningDrawer.tsx, AppShell.tsx, frontend/app/layout.tsx,
frontend/public/manifest.webmanifest (novo), frontend/app/perfil/page.tsx,
frontend/app/importar/page.tsx

TESTES / VALIDAÇÃO
  cd frontend && npx tsc --noEmit && npm run lint && npm run build
Conferir na tabela do build que /relatorios caiu. Navegar os três diálogos só
com teclado: foco entra, Tab circula dentro, Escape fecha, foco volta à origem.

CRITÉRIOS DE ACEITE
- /relatorios com First Load JS abaixo de 140 kB (hoje 240 kB), comprovado pela
  saída do next build colada no PR.
- Um erro lançado propositalmente numa página mostra a tela de erro do Trevo.
- Os três diálogos passam no teste de teclado descrito acima.
- manifest.webmanifest válido; app instalável no Chrome mobile.
- tsc, lint e build limpos; nenhuma outra rota regride em tamanho.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados, e nada em app/ (backend).
```

---

### PROMPT BP-08 — Schema e migrations

```
Projeto Trevo (FastAPI + PostgreSQL/Supabase). ESTE É O BREAKPOINT DE MAIOR
RISCO OPERACIONAL. Leia a seção de riscos antes de começar.

SITUAÇÃO ATUAL
O schema vive em DOIS lugares: SCHEMA_SQL (≈175 linhas de DDL idempotente
dentro de migrate.py) e migrations/*.sql versionadas. Há sobreposição real:
login_failures_state é criada em SCHEMA_SQL E em migrations/0005;
categories.is_active e categories.updated_at aparecem em SCHEMA_SQL E em
0006/0010. O baseline nunca foi congelado — ele cresce junto com as migrations.

DEFEITO OPS-01
app/main.py::ensure_serverless_schema roda uma vez por processo (primeiro
request de /api/) e chama run_migrations_locked → run_migrations →
cursor.execute(SCHEMA_SQL). Ou seja, a CADA COLD START o servidor executa:
  CREATE OR REPLACE FUNCTION set_updated_at()
  DROP TRIGGER / CREATE TRIGGER (vários)
  ALTER TABLE transactions DROP CONSTRAINT IF EXISTS transactions_source_check
  ALTER TABLE transactions ADD CONSTRAINT transactions_source_check CHECK (...)
ADD CONSTRAINT pega ACCESS EXCLUSIVE em transactions e revalida a tabela. Tudo
isso sob pg_advisory_lock. Consequências: latência de cold start, serialização
de instâncias paralelas, e uma janela em que a constraint de source não existe.

DEFEITO CI-03
vercel.json não tem nenhum passo de build que execute migrations. O contorno em
runtime (acima) é o que produz OPS-01.

OUTROS DEFEITOS
- DB-01: card_pins (migrate.py:110) referencia cards(id) e users(id)
  SEPARADAMENTE, em vez da FK composta (user_id, card_id) → cards(user_id, id)
  usada por todas as outras tabelas. É a única exceção à invariante de
  isolamento do projeto.
- DB-02: migrations/0007 não existe. A sequência é 0001-0006, 0008-0012, e
  git log --diff-filter=D -- migrations/ não mostra remoção.
- DB-05: transactions não tem updated_at (users, categories e budgets têm,
  com trigger set_updated_at).
- FIN-03: calculate_score identifica reservas por
  `WHERE lower(c.name) IN ('reserva','investimentos')`. Renomear a categoria
  zera silenciosamente esse eixo do Ritmo Score.
- DB-08: list_transactions tem LIMIT 250 fixo, sem paginação e sem indicar ao
  cliente que houve truncamento.

ESCOPO
1. Extrair SCHEMA_SQL para migrations/0000_baseline.sql, BYTE A BYTE. Remover
   SCHEMA_SQL de migrate.py.
2. Criar script idempotente que REGISTRA 0000 como aplicado em bancos que já têm
   o schema (INSERT ... ON CONFLICT DO NOTHING em schema_migrations), sem
   executar DDL. Ele precisa detectar corretamente um banco já provisionado.
3. ensure_serverless_schema passa a apenas VERIFICAR (SELECT em
   schema_migrations) e logar WARNING se houver migration pendente. Nunca aplicar.
4. vercel.json: adicionar buildCommand que executa `python migrate.py`. Documentar
   o fluxo em docs/guides/deployment.md.
5. Migration: ALTER TABLE card_pins com FK composta
   (user_id, card_id) REFERENCES cards(user_id, id) ON DELETE CASCADE.
   Incluir na própria migration uma verificação prévia que RAISE EXCEPTION se
   houver linha inconsistente, com mensagem clara.
6. Migration: transactions.updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW() +
   trigger transactions_set_updated_at.
7. Migration: categories.role TEXT ('reserve'|'investment'|'income'|'other') com
   backfill das categorias padrão. calculate_score passa a usar role.
8. Paginação em GET /api/transactions: limit/offset com hasMore na resposta.
   Atualizar frontend/lib/api.ts e a tela de transações.
9. Criar migrations/README.md com a convenção de numeração e a conclusão sobre a
   ausência de 0007 (verifique schema_migrations em produção antes de escrever).

RESTRIÇÕES
- NÃO migre transaction_date/billing_month de TEXT para DATE (fora de escopo;
  decisão registrada como P3).
- NÃO adicione transferência, estorno nem conta bancária.
- NÃO altere dados de usuários em nenhuma migration, exceto o backfill de
  categories.role em categorias com is_default = 1.
- Toda migration deve ser idempotente (IF NOT EXISTS / IF EXISTS).

VALIDAÇÃO OBRIGATÓRIA ANTES DE APLICAR EM PRODUÇÃO
Restaure um dump de produção num banco descartável e rode:
  python migrate.py
  psql "$DATABASE_URL" -c "SELECT version FROM schema_migrations ORDER BY version"
  psql "$DATABASE_URL" -c "\d+ card_pins"
  psql "$DATABASE_URL" -c "\d+ transactions"
Depois, num banco VAZIO:
  python migrate.py   # deve criar tudo a partir de 0000 + versionadas
E a suíte:
  pytest tests/ -q

CRITÉRIOS DE ACEITE
- SCHEMA_SQL não existe mais em migrate.py.
- Num banco já provisionado, migrate.py não aplica DDL e não erra.
- Num banco vazio, migrate.py cria o schema completo.
- ensure_serverless_schema não executa DDL (comprovado com o contador de queries
  do BP-06).
- \d card_pins mostra a FK composta.
- Deploy na Vercel executa as migrations no build.
- calculate_score não referencia mais nomes literais de categoria.
- migrations/README.md registra a conclusão sobre 0007.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados. Se a verificação de card_pins revelar
dados inconsistentes, PARE e relate — não corrija dados automaticamente.
```

---

### PROMPT BP-09 — Extração de roteadores por domínio

```
Projeto Trevo (FastAPI). app/main.py tem 5.133 linhas e concentra as 56 rotas,
as regras de negócio, o SQL e um gerador de PDF de ~420 linhas. As oito pastas
de domínio (app/auth, app/transactions, app/budgets, app/cards, app/categories,
app/dashboard, app/goals, app/imports, app/reports, app/users) existem com um
__init__.py de UMA LINHA cada, reservadas exatamente para isto.

ESTA REFATORAÇÃO É ESTRITAMENTE MECÂNICA. Nenhuma mudança de comportamento.

ANTES DE COMEÇAR
Capture o baseline do contrato da API:
  python -c "from app.main import app; import json; print(json.dumps(app.openapi()))" > /tmp/openapi-antes.json
Você vai comparar contra ele depois de cada commit.

ESTRUTURA-ALVO
app/
├── core/            config · database · security · signing · storage · logging
│                    (remova errors.py e middleware.py — ambos sem importadores)
├── shared/          money · dates · clock
├── api/
│   ├── deps.py      get_current_user, PlainDictRoute, request_cached
│   └── middleware.py  request_id · content-type · csrf · security headers
├── core/ephemeral.py  csv_import_sessions, card_unlock_sessions,
│                      revoked_token_hashes, login_failures, card_pin_failures
│                      (fallbacks de processo; documente isso no módulo)
├── auth/ transactions/ cards/ budgets/ categories/ goals/ dashboard/ reports/
│   imports/ privacy/   cada um com router.py · service.py · repository.py ·
│                       schemas.py
└── main.py          app, middlewares, include_router, lifespan — < 200 linhas

REGRAS DE DEPENDÊNCIA (verificáveis por grep de imports)
- core e shared NÃO importam de nenhum domínio.
- Domínios importam de core e shared livremente.
- Domínios NÃO importam uns dos outros, EXCETO dashboard e reports, que são
  composição de leitura e podem importar os service.py dos demais.
- main.py importa apenas routers.

ORDEM — UM COMMIT POR ITEM, suíte verde em cada um
1. app/api/middleware.py e app/api/deps.py, app/core/ephemeral.py
2. app/auth/ (register, login, logout, me, csrf, change-password, stats,
   oauth, delete account, avatar)
3. app/privacy/router.py (export, consent) — service.py já existe
4. app/categories/
5. app/cards/ (PIN, unlock, simulação, parcelas com cartão)
6. app/transactions/ (parcelas sem cartão, recorrência, sugestões)
7. app/budgets/ (inclui regras de categorização)
8. app/goals/
9. app/imports/ (mova o parsing para app/imports/parsers/)
10. app/reports/ (mova o gerador de PDF inteiro para app/reports/pdf.py)
11. app/dashboard/ (bootstrap, score, alerts — composição)
12. main.py final

RESTRIÇÕES — CRÍTICAS
- NENHUMA mudança de comportamento. Nenhuma correção de bug, nenhuma
  otimização, nenhuma mudança de assinatura de rota, de nome de campo ou de
  código de status. Se encontrar um bug, ANOTE no PR e siga.
- O openapi.json deve permanecer IDÊNTICO ao baseline. Compare a cada commit:
  diff <(jq -S . /tmp/openapi-antes.json) <(jq -S . /tmp/openapi-depois.json)
- Cuidado com importação circular. A regra de dependência acima existe para
  evitá-la; dashboard e reports são os únicos que podem importar outros
  domínios, e só os service.py.
- O estado global compartilhado (csv_import_sessions, card_unlock_sessions,
  revoked_token_hashes, login_failures, card_pin_failures) deve ir para
  app/core/ephemeral.py, com um docstring explicando que são fallbacks de
  processo usados quando o Postgres não está disponível.
- NÃO remova PlainDictRoute nem request_cached. Ambos resolvem problemas reais
  documentados no próprio código.

VALIDAÇÃO A CADA COMMIT
  pytest tests/ -q
  ruff check .
  python -c "from app.main import app; print(len(app.routes))"
  diff <(jq -S . /tmp/openapi-antes.json) <(jq -S .  <(python -c "from app.main import app; import json; print(json.dumps(app.openapi()))"))

CRITÉRIOS DE ACEITE
- wc -l app/main.py < 200
- openapi.json idêntico ao baseline (mesmas 56 rotas, mesmos métodos e parâmetros)
- Suíte completa verde em CADA commit intermediário
- Nenhuma pasta de domínio com __init__.py vazio
- app/core/errors.py e app/core/middleware.py removidos
- Regras de dependência documentadas em docs/architecture/overview.md e
  respeitadas (nenhum import cruzado entre domínios, exceto dashboard/reports)

REGRA DE ESCOPO
Este breakpoint move código. Não altera código.
```

---

### PROMPT BP-10 — Dependências e limpeza técnica

```
Projeto Trevo. Limpeza de dependências, código morto e resíduos de duas marcas
anteriores (o projeto já se chamou "ritmo" e "pulsa" antes de "trevo").

DEFEITOS
DEP-01 — requirements.txt mistura produção e teste: pytest, pytest-asyncio,
pytest-cov, factory-boy, freezegun, bandit e ruff estão lá. A Vercel instala
desse arquivo, então tudo isso vai para o bundle serverless.
DEP-02 — lucide-react (^0.468.0) em frontend/package.json tem ZERO importações
(verificado por grep em app/, components/ e lib/). Os ícones vêm de
@fortawesome via components/icons.tsx.
DEP-03 — freezegun não é usado por nenhum teste.
DEP-04 — python-json-logger não é importado; app/core/logging.py implementa o
formatter à mão.
DEP-05 — supabase==2.30.1 (+ supabase-auth, supabase-functions, storage,
postgrest, realtime, gotrue, websockets, httpx[http2]) está instalada para
exatamente TRÊS chamadas em app/core/storage.py: upload, create_signed_url e
remove. httpx já é dependência direta.
DEP-06 — requirements.txt tem `bcrypt<4.1` porque passlib 1.7.4 (último release
em 2020, efetivamente sem manutenção) quebra com bcrypt >= 4.1. A biblioteca de
hash de senha de um app financeiro está congelada em outubro de 2023 por causa
de um wrapper abandonado.
DEP-07 — python-jose traz ecdsa==0.19.2, com PYSEC-2026-1325 sem correção
publicada. O CI ignora com justificativa correta (os tokens são HS256). PyJWT
não traz ecdsa nem rsa.
DEP-08 — pydantic não tem teto de versão (vem transitivo do FastAPI) e os 22
modelos usam `class Config` do Pydantic v1, deprecado no v2. Um Pydantic v3
removeria o suporte e `extra = "forbid"` deixaria de valer SILENCIOSAMENTE em
todos os payloads.
DEAD-01 — app/core/errors.py (helpers bad_request/not_found, zero usos),
app/core/middleware.py (só uma docstring), app/integrations/base.py
(ImportedTransaction e FinancialDataSource, zero importações).
7.1 — o gerador de PDF em app/reports/pdf.py (movido para lá no BP-09) tem ~420
linhas escritas byte a byte e faz `.encode("latin-1", "replace")`, o que
transforma QUALQUER caractere fora de latin-1 em "?" — num produto em português
com nomes de categoria livres e emoji nos ícones.
CFG-01 — render.yaml está quebrado (env: python, buildCommand só instala pip,
nunca constrói o frontend) e o nome do serviço ainda é "ritmo-financeiro-pro".
CFG-02 — docker-compose.yml e o CI usam usuário/banco "ritmo".
CFG-03 — CategoryPayload.color tem default "#9be768", o verde da marca ANTERIOR
(o atual é #2E9D5B).
CFG-05 — Makefile lint roda `ruff check app/ tests/`, enquanto o CI roda
`ruff check .` — o comando local não cobre migrate.py, api/ nem main.py.
CFG-06 — a versão "2.0.0" está escrita em três lugares (FastAPI(version=...) e
duas vezes no handler de /api/health) e o CHANGELOG não tem nenhuma versão
publicada.
DOC-01 — app/integrations/open_finance/README.md cita um TransactionNormalizer
que não existe no código.
CI-02 — o job build-and-push publica imagem Docker no GHCR a cada push em main,
e nada consome essa imagem (o deploy é Vercel).

ESCOPO E ORDEM (uma coisa por commit; as três últimas são as arriscadas)
1. Separar requirements.txt (produção) de requirements-dev.txt. Atualizar CI,
   Dockerfile e documentação.
2. Remoções triviais: lucide-react, freezegun, python-json-logger,
   app/core/errors.py, app/core/middleware.py. Mover app/integrations/base.py
   para documentação OU deixá-lo com um docstring dizendo que é especificação,
   não código vivo (ele é o contrato de desenho do Open Finance).
3. render.yaml removido; build-and-push condicional a tag de release (ou
   removido); "ritmo" → "trevo" em docker-compose.yml e no CI;
   CategoryPayload.color → "#2E9D5B"; Makefile lint → `ruff check .`; versão
   unificada numa constante única; primeira tag no CHANGELOG;
   open_finance/README.md corrigido.
4. pydantic: fixar `pydantic>=2.9,<3` e migrar os 22 `class Config` para
   `model_config = ConfigDict(extra="forbid")`.
5. passlib → bcrypt direto em app/core/security.py, liberando bcrypt para a
   versão atual. O formato $2b$ é o mesmo; os hashes existentes continuam válidos.
6. python-jose → PyJWT em app/core/security.py e app/oauth.py.
7. supabase → três chamadas httpx à Storage REST API em app/core/storage.py
   (POST /storage/v1/object/{bucket}/{path}, POST /storage/v1/object/sign/...,
   DELETE), com header Authorization: Bearer <service_role_key>.
8. Gerador de PDF → fpdf2, resolvendo a corrupção de acentos.

RESTRIÇÕES
- NÃO mude comportamento de produto.
- Os itens 5, 6 e 8 mudam coisas com risco de indisponibilidade total. Um commit
  cada, com teste dedicado.
- Item 5: TESTE OBRIGATÓRIO de que um hash bcrypt gerado pela versão antiga
  continua verificando. Se o formato divergir, ninguém consegue logar.
- Item 6: TESTE OBRIGATÓRIO de que um JWT emitido pela versão antiga continua
  decodificando.
- bcrypt moderno REJEITA senhas acima de 72 bytes em vez de truncar.
  validate_password_strength já valida 72 bytes, então está coberto — confirme.
- Item 8 muda a aparência do relatório. Compare antes/depois manualmente e anexe
  ambos ao PR.

ARQUIVOS
requirements.txt, requirements-dev.txt (novo), requirements-lock.txt, Dockerfile,
.github/workflows/ci.yml, app/core/security.py, app/core/signing.py,
app/oauth.py, app/core/storage.py, app/reports/pdf.py, app/main.py (Config →
ConfigDict, versão), frontend/package.json, docker-compose.yml, render.yaml
(remoção), Makefile, CHANGELOG.md, app/integrations/

VALIDAÇÃO
  pip install -r requirements.txt   # não deve instalar pytest, ruff, bandit
  pytest tests/ -q
  pip-audit -r requirements.txt --strict
  pip-audit -r requirements-lock.txt --strict
  cd frontend && npm ci && npm run build

CRITÉRIOS DE ACEITE
- requirements.txt não contém dependência de teste ou lint.
- Hash bcrypt antigo verifica (teste).
- JWT antigo decodifica (teste).
- `pip freeze | grep ecdsa` não retorna nada.
- PDF gerado mostra "Alimentação" e "Saúde" com acentos corretos.
- `grep -ri ritmo --include="*.yml" --include="*.yaml" .` não retorna nada.
- Bundle do frontend não regride (comparar a tabela do next build).
- pytest, ruff, tsc e build limpos.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados.
```

---

### PROMPT BP-11 — CI/CD, observabilidade e operação

```
Projeto Trevo. Deploy na Vercel; banco no Supabase (us-east-2); CI em
.github/workflows/ci.yml.

DEFEITOS
CI-06 — nenhum error tracking. Existem logs estruturados
(app/core/logging.py::JsonLogFormatter) e X-Request-Id em toda resposta, mas
nada os coleta. Um 500 em produção só é descoberto por reclamação.
CI-07 — nenhuma política de backup verificada. docs/security/ tem checklist e
plano de resposta a incidente, mas não existe procedimento de RESTORE testado.
São 27 MB de dados financeiros de terceiros.
CI-01 — o passo "Falhar se algum teste foi pulado" REEXECUTA a suíte inteira e
faz grep na saída; em caso de falha, executa uma terceira vez para listar.
CI-04 — sem concurrency group: pushes em sequência disparam builds paralelos.
CI-05 — sem npm audit no frontend, embora o Dependabot esteja configurado.
SEC-13 — bandit roda com -ll, então achados LOW nunca aparecem. Não há outro SAST.
SEC-15 — pip-audit audita requirements.txt, mas o Docker instala
requirements-lock.txt. O artefato publicado como imagem não é auditado.

ESCOPO
1. Integrar Sentry (ou equivalente) no backend e no frontend.
   ATENÇÃO CRÍTICA: configure before_send com denylist EXPLÍCITA de e-mail,
   valores monetários, conteúdo de CSV, tokens e cookies. Este é um app
   financeiro; um Sentry mal configurado vaza dados de titulares para um
   terceiro. Use X-Request-Id como tag. Ligue o Sentry do frontend aos error
   boundaries criados no BP-07.
   TESTE o scrubbing com um erro proposital carregando payload sensível ANTES de
   habilitar em produção.
2. Documentar e EXECUTAR um restore do backup automático do Supabase num projeto
   descartável. Registrar em docs/guides/operations.md: o procedimento passo a
   passo, o RTO e o RPO MEDIDOS (não estimados), e a data do teste. Agendar
   verificação trimestral.
3. Substituir o passo de "falhar se pulou" por um hook pytest_collection_modifyitems
   (ou pytest_runtest_logreport) em tests/conftest.py que ERRA quando
   TEST_DATABASE_URL está definida e um teste é pulado. Remover as duas
   reexecuções do CI.
4. Adicionar concurrency group ao workflow (group por ref, cancel-in-progress).
5. Adicionar `npm audit --audit-level=high` ao passo de frontend.
6. Adicionar pip-audit sobre requirements-lock.txt.
7. bandit sem -ll (ou com -l), gerando um baseline no mesmo commit para os LOW
   pré-existentes.
8. Configurar um uptime check externo apontando para /api/health, com alerta.

RESTRIÇÕES
- NÃO introduza APM completo, tracing distribuído nem dashboards de métricas.
  Desproporcional ao porte do projeto.
- NÃO habilite o Sentry em produção antes de validar o scrubbing.
- O hook de skip NÃO pode falhar quando TEST_DATABASE_URL está ausente (o
  desenvolvimento local sem banco deve continuar funcionando, com o aviso que já
  existe em pytest_report_header).

ARQUIVOS
.github/workflows/ci.yml, tests/conftest.py, app/main.py (init do Sentry),
frontend/app/layout.tsx, requirements.txt, frontend/package.json,
docs/guides/operations.md, docs/security/incident-response.md

VALIDAÇÃO
  pytest tests/ -q                          # sem TEST_DATABASE_URL: avisa, não falha
  TEST_DATABASE_URL=... pytest tests/ -q    # skip deve FALHAR a suíte
Disparar um erro proposital em staging e verificar que chega ao Sentry SEM
e-mail, SEM valores monetários e SEM conteúdo de CSV.

CRITÉRIOS DE ACEITE
- Erro em produção aparece no coletor em menos de 1 minuto, com request_id e sem
  PII (comprovado com screenshot ou log no PR).
- docs/guides/operations.md registra um restore EXECUTADO, com data e duração.
- O CI roda a suíte UMA vez.
- npm audit e pip-audit (sobre o lock) no pipeline.
- Uptime check ativo com alerta configurado.

REGRA DE ESCOPO
Não altere nada fora dos arquivos listados.
```

---

### PROMPT BP-12 — Open Finance: decisão e fundação

```
Projeto Trevo. A implementação atual de Open Finance é um espaço reservado:
app/integrations/base.py define ImportedTransaction e o Protocol
FinancialDataSource (e NÃO é importado por ninguém), e o valor
'open_finance_future' é aceito na constraint de transactions.source. Não há
autenticação, consentimento, certificados nem chamada a instituição alguma.

CONTEXTO REGULATÓRIO (verificado e correto)
No Brasil, consumir as APIs de Open Finance exige ser instituição autorizada
pelo Banco Central, com certificados ICP-Brasil, registro no diretório de
participantes e conformidade com um regime de segurança específico. Para um
produto neste estágio, o único caminho viável é intermediar por um AGREGADOR
LICENCIADO (Pluggy, Belvo). Isso muda a natureza do trabalho de "implementar um
protocolo" para "integrar um fornecedor e modelar consentimento, sincronização e
expiração".

FASE 1 — DECISÃO (este prompt). NENHUM código de integração.
Produza docs/architecture/adr-open-finance.md contendo:
1. Comparação de Pluggy e Belvo: cobertura de instituições brasileiras, custo
   real por conexão e por mês, modelo de consentimento, SLA, e quais requisitos
   de conformidade são repassados ao integrador.
2. Modelagem de domínio no papel (sem migration):
   - connections: instituição, status, consentimento, validade, user_id
   - sync_runs: lote, janela sincronizada, resultado, contagens
   - reaproveitamento de transactions.import_batch_id e duplicate_hash, que já
     existem
   - FK composta (user_id, id) como em todo o resto do schema
3. Política de expiração de consentimento (tipicamente 12 meses) e de revogação:
   o que acontece com os dados já importados?
4. Política de reconciliação: transação pendente que vira consolidada;
   transação que desaparece do extrato; transação que muda de valor.
5. Decisão explícita GO / NO-GO / ADIAR, com data de reavaliação.
6. Revisar a copy do produto para não prometer conexão bancária que não existe.
   A documentação já é honesta nisso; a interface deve ser.

FASE 2 — só execute se a fase 1 decidir GO
- app/integrations/<fornecedor>/ implementando o contrato de base.py
- Migrations de connections e sync_runs
- Sincronização SOB DEMANDA (não agendada) — evita introduzir fila
- Reuso do pipeline de normalização e deduplicação já construído para o CSV

RESTRIÇÕES
- NÃO tente falar direto com o Open Finance brasileiro.
- NÃO implemente sincronização agendada em background nem webhooks nesta fase.
- NÃO comece a fase 2 sem a fase 1 concluída e aprovada — fazê-lo produz uma
  integração amarrada a um fornecedor antes de a escolha ser feita.
- O custo por conexão pode inviabilizar o produto no estágio atual. Por isso ele
  entra no ADR ANTES do código.

ARQUIVOS
docs/architecture/adr-open-finance.md (novo), docs/product/open-finance.md,
app/integrations/, frontend/ (apenas copy)

CRITÉRIOS DE ACEITE
- ADR versionado com decisão explícita (incluindo "não fazer agora", se for o
  caso) e data de reavaliação.
- Nenhuma tela do produto promete conexão bancária que não existe.
- Nenhuma dependência nova adicionada na fase 1.

REGRA DE ESCOPO
A fase 1 não escreve código de aplicação. Apenas documentação e ajuste de copy.
```

---

# 18. Recommended Execution Order

A ordem abaixo pressupõe uma pessoa trabalhando. Se houver duas, a trilha do frontend (BP-07) pode correr em paralelo desde o início — é a única que não toca `app/main.py`.

| Ordem | Breakpoint | Por que agora | Estimativa |
|---|---|---|---|
| 1 | **BP-00** — Baseline | Nada mais deve ser tocado sem rede. Os `xfail` viram o placar do plano inteiro. | M |
| 2 | **BP-01** — Segurança de sessão | Os dois P0 de segurança. O primeiro (cache) é uma função e duas chamadas: maior razão impacto/esforço do plano. | M |
| 3 | **BP-02** — Integridade destrutiva | O P0 restante. Enquanto não for corrigido, cada importação em "substituir" é uma perda de dados possível. | M |
| 4 | **BP-03** — Domínio financeiro | Os números do produto passam a estar certos. Exige a decisão sobre DOM-05 antes de começar. | L |
| 5 | **BP-04** — CSV | Depende de BP-02. Destrava extratos reais e resolve o timeout. | L |
| 6 | **BP-07** — Frontend | Independente; encaixa aqui se houver só uma pessoa. Boundaries e `/relatorios`. | M |
| 7 | **BP-06** — Performance | Consolida os N+1 restantes antes de a extração mover o código. | L |
| 8 | **BP-05** — Auth e OAuth | Depende de BP-01. Menos urgente porque o risco imediato de SEC-04 é baixo. | L |
| 9 | **BP-08** — Schema | Agrupa as migrations de BP-02 e BP-04 numa rodada só, e fecha OPS-01 e CI-03. Maior risco operacional — fazer com a suíte já robusta. | L |
| 10 | **BP-11** — CI/CD e operação | Depende de BP-08. Error tracking e backup testado — deveriam vir antes, e viriam, se não dependessem do passo de deploy. **Se preferir, antecipe só o Sentry e o teste de backup para logo após BP-01.** | M |
| 11 | **BP-09** — Extração de roteadores | Só depois que tudo o que ia mudar mudou. Mover código corrigido, não código a corrigir. | XL |
| 12 | **BP-10** — Dependências e limpeza | Depende de BP-09 (PDF isolado). | L |
| 13 | **BP-12** — Open Finance (fase 1) | Decisão documentada, sem código. | S |

## Ajuste recomendado se a operação preocupar mais que a dívida

Sentry e teste de restore de backup (as duas primeiras tarefas do BP-11) não dependem tecnicamente de BP-08 — só ficariam melhor depois. Se a prioridade for "não perder dados e saber quando algo quebra", **antecipe essas duas tarefas para logo depois do BP-01**, deixando o resto do BP-11 na posição 10.

## Marcos

- **Depois do BP-03:** todo P0 fechado e os números do produto corretos. É o ponto natural para uma primeira tag de release.
- **Depois do BP-08:** o sistema está operacionalmente sólido — schema congelado, migrations no deploy, backup testado.
- **Depois do BP-10:** a dívida técnica está paga. Daqui em diante o trabalho volta a ser produto.

---

# 19. Deferred / Not Worth Doing

## Adiado com data de reavaliação

| Item | Por que adiar | Reavaliar quando |
|---|---|---|
| **`transaction_date`/`billing_month` de `TEXT` para `DATE`** (DB-04) | Funciona: ISO-8601 ordena lexicograficamente e o índice de expressão de 0012 cobre as agregações. A migração toca ~15 queries, o índice de expressão e o contrato da API. Custo grande, ganho hoje pequeno. | Depois do BP-09, quando as queries estiverem em `repository.py` por domínio e a mudança for localizável. Ou antes, se surgir a necessidade de aritmética de data em SQL. |
| **Transferência, estorno e conta bancária como primitivas** (FIN-04) | É o maior gap de produto do Trevo frente aos comparáveis (seção 12), e vai precisar ser feito. Mas é modelagem nova, migração de dados e retrabalho em todas as telas — não cabe junto com correção de defeito. | Depois do BP-08, como projeto de produto próprio, com seu próprio ADR. |
| **MFA / TOTP** | É a única lacuna de autenticação em que o Trevo fica atrás dos comparáveis. Mas SEC-01, SEC-02 e SEC-04 são mais explorados hoje do que a ausência de MFA. | Depois do BP-05. |
| **Data de competência** (FIN-05) | O par (data da compra, mês de fatura) cobre o caso dominante. A competência só importa quando houver despesa paga em M referente a M-1 de forma sistemática. | Junto com FIN-04 — são o mesmo trabalho de modelagem. |
| **Perfis de importação por banco** (mapeamento salvo por instituição) | Melhoria clara, mas BP-04 já resolve o que impede a importação de funcionar. Perfil salvo é conveniência. | Depois do BP-04, se o uso mostrar que as pessoas reimportam do mesmo banco. |
| **E2E com Playwright** (TEST-02) | O BP-00 cobre o risco maior (caminho cookie+CSRF) com testes de integração, que são muito mais baratos. | Depois do BP-09, quando a suíte estiver estável e a estrutura parada. |
| **Múltiplos provedores sociais por usuário** | Limitação real, impacto baixo com 13 usuários. | Quando alguém reclamar. |
| **Parcelamento acima de 24x** (FIN-11) | Uma constante. Trivial, mas sem urgência. | Junto com qualquer outro trabalho em cartões. |

## Não fazer

| Item | Por quê |
|---|---|
| **Microserviços** | Um domínio, um banco, 13 usuários. Roteadores por domínio no mesmo processo (BP-09) entregam todo o benefício de fronteira sem nenhum custo de rede, deploy ou consistência distribuída. |
| **Fila / worker / broker** | A única operação longa é a importação de CSV, e ela fica curta ao corrigir PERF-01. Introduzir fila para não corrigir um N+1 é trocar um problema de 200 linhas por um componente de infraestrutura permanente. |
| **Redis ou cache distribuído** | Não há gargalo de leitura repetida *entre* requests. O caso real — agregações repetidas *dentro* de uma request — já é resolvido por `request_cached` com `ContextVar`, que não pode servir dado velho. Estender isso (BP-06) custa dez linhas. |
| **Event sourcing / CQRS** | Nenhuma necessidade de projeções múltiplas nem de reconstrução temporal. `updated_at` (BP-08) resolve a rastreabilidade real. |
| **Kubernetes / orquestração** | Vercel cobre produção, Docker cobre dev. Nenhum problema atual é de orquestração. |
| **Migrar para um ORM (SQLAlchemy)** | O SQL cru é legível, parametrizado e explícito sobre as FKs compostas que são a melhor decisão do projeto. Um ORM custaria semanas e apagaria essa clareza. |
| **Open Finance direto (sem agregador)** | Exige autorização do Banco Central, ICP-Brasil e registro no diretório de participantes. Fora de alcance por razões regulatórias, não técnicas. |
| **Substituir recharts** | 121 kB é muito para uma página, mas o problema é *quando* a biblioteca carrega, não qual é. `dynamic()` resolve, e o padrão já está provado no dashboard. |
| **Reescrever o frontend em outra coisa** | O Next.js static export resolve o problema (SPA servida por CDN, API separada) com o custo de aceitar `'unsafe-inline'` na CSP. Aceitável e documentado. |
| **Multi-moeda de verdade** | `settings.currency` existe, tem default `'BRL'` e nenhuma query ou formatação a consulta (FIN-07). Implementar de verdade é desproporcional: não há usuário pedindo. A ação correta é a mais barata — documentar a coluna como reservada num comentário de schema, junto com as três colunas de reserva (DB-06), no BP-08. |
| **Categorização por ML** | As regras por substring resolvem o caso comum e são explicáveis ao usuário. ML aqui seria complexidade sem problema correspondente. |
| **Sair da Vercel para container** | Resolveria PERF-04, SEC-02 e OPS-01 de uma vez, e é tentador. Mas custa operação e dinheiro, e os três têm correção local viável (BP-06, BP-01, BP-08). Reavaliar só se a carga crescer a ponto de a conexão-por-request virar gargalo mesmo depois do BP-06. |

---

# 20. Final Assessment

## Estado atual

O Trevo é um produto funcional em produção, com 13 usuários reais e dados reais. A qualidade do que está escrito é consistentemente boa: sem código morto significativo no caminho quente, sem `TODO`s acumulados, sem `any` no TypeScript, com validação rigorosa, com dinheiro em `Decimal` e com uma decisão de modelagem — as FKs compostas — que é melhor do que a de vários comparáveis maiores. Os comentários explicam *por que*, não *o que*, e isso se paga toda vez que alguém volta ao código.

O que a auditoria encontrou não contradiz isso. Encontrou o padrão típico de um sistema construído com cuidado por uma pessoa: **o que estava no campo de visão está bem feito, e o que estava fora dele não está**. O cache do frontend foi projetado para deduplicar requests, e a identidade do usuário nunca entrou na chave porque a sessão mora no cookie — a lógica é correta e a conclusão é errada. O rate limiting foi implementado antes do deploy serverless e nunca foi reavaliado depois. A importação em "substituir" foi escrita pensando em "substituir o que importei", e o `DELETE` não sabe disso.

## Dívida técnica

Em ordem de custo real:

1. **`app/main.py` com 5.133 linhas.** É a dívida que multiplica todas as outras. Cada um dos 13 breakpoints acima tocaria esse arquivo se pudesse — e por isso o plano os serializa. Extrair os roteadores (BP-09) não corrige nenhum bug, mas é o que torna o próximo ano de trabalho barato.
2. **Duas fontes de verdade de schema.** `SCHEMA_SQL` cresce junto com as migrations, e roda a cada cold start executando `ALTER TABLE ... ADD CONSTRAINT` numa tabela de produção.
3. **Dependências.** `passlib` abandonado prendendo `bcrypt` em 2023 é a mais séria: é a biblioteca de hash de senha de um app financeiro, congelada para acomodar um wrapper que ninguém mantém.
4. **Ausência de observabilidade e de backup testado.** A instrumentação está pronta (`JsonLogFormatter`, `X-Request-Id`, health separado em liveness/readiness) e não tem destino. Para dados financeiros de terceiros, é a lacuna operacional mais séria do projeto.

## Maturidade arquitetural

Num espectro de 1 a 5:

| Dimensão | Nota | Comentário |
|---|---|---|
| Modelagem de dados | **4** | FKs compostas, `NUMERIC(14,2)`, índices pensados. Falta `updated_at`, `card_pins` é a exceção, e faltam transferência/estorno. |
| Segurança | **3,5** | Acima da média em quase tudo; três lacunas de impacto alto e sem MFA. |
| Organização do código | **2** | O monólito. As pastas existem e estão vazias — a intenção está registrada, a execução não. |
| Domínio financeiro | **3** | Correto no essencial (decimal, centavos, sinal pelo tipo); ambíguo no que importa (renda, fuso, fechamento avulso). |
| Testes | **2,5** | 80 testes que passam, e o caminho de produção não é um deles. |
| Frontend / UX / A11y | **4** | O ponto mais forte depois da modelagem. Design system real, contraste calculado, padrões acessíveis. Faltam boundaries. |
| Operação | **2** | Deploy funciona; nada mais existe. |
| Documentação | **4** | `docs/` é organizado, honesto sobre limitações, e a auditoria anterior tem o hábito raro de se corrigir em público. |

## Principal caminho de evolução

**Não é Open Finance.** A auditoria anterior tratou o Open Finance como a grande lacuna do produto, e o benchmarking da seção 12 discorda: os comparáveis brasileiros que têm Open Finance chegaram lá *depois* de modelar conta bancária e transferência. O Trevo ainda não modela nenhuma das duas, e por isso mover dinheiro entre contas hoje infla receita e despesa ao mesmo tempo. **O maior gap de produto é o modelo de domínio, não a integração.**

O caminho, em três estágios:

1. **Curto prazo (BP-00 a BP-08).** Fechar os P0, corrigir os números, fazer a importação funcionar com extratos reais, e colocar backup e error tracking de pé. Ao fim disso o produto está correto e operável.
2. **Médio prazo (BP-09, BP-10).** Extrair os roteadores e pagar a dívida de dependências. Ao fim disso o produto está manutenível — e só aí faz sentido acelerar o ritmo de features.
3. **Longo prazo.** Modelar conta e transferência (FIN-04), adicionar MFA, e só então avaliar Open Finance via agregador — com o ADR do BP-12 já escrito e o custo já conhecido.

A ordem importa mais do que o conteúdo. Fazer Open Finance antes de modelar transferência produziria transações sincronizadas que o domínio não sabe representar. Fazer a extração de roteadores antes das correções significaria mover código errado. Fazer as correções sem o BP-00 significaria não saber se funcionaram.

---

*Documento produzido em 15 de setembro de 2026 contra o commit `b9d368e`. As afirmações marcadas CONFIRMADO têm evidência de código, de comando executado ou de build citada no corpo do texto. As que dependem de acesso ao banco de produção — notadamente o estado de `schema_migrations` e a ausência de `0007` — estão marcadas como não verificáveis e entram no plano como investigação, não como conclusão.*
