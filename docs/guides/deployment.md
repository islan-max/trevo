# Deploy e ambientes

## Vercel (deploy primário)

Projeto único unificado via **Services** (recurso em beta da Vercel — veja o
aviso abaixo): o frontend Next é o serviço `frontend` (estático, `output:
"export"`) e o FastAPI é o serviço `api` (Python, `api/index.py`), na
**mesma origem** — o cookie de sessão funciona sem CORS. Config em
`vercel.json`; ambos os serviços fazem deploy juntos, roteados por `rewrites`
no nível raiz do arquivo.

### BP-08 (OPS-01/CI-03): migrations rodam no build, não a cada cold start

Antes, `ensure_serverless_schema` aplicava as migrations no primeiro request
de cada instância fria — em serverless isso significava reexecutar DDL
(incluindo um `DROP CONSTRAINT`+`ADD CONSTRAINT` que trava `transactions`
inteira) toda vez que uma instância nova acordava. Agora o serviço `api` tem
`"buildCommand": "python migrate.py"`: as migrations aplicam uma vez, no
build, antes do deploy ficar no ar. Em runtime, `ensure_serverless_schema`
só VERIFICA se o schema está em dia (`SELECT` em `schema_migrations`) e
loga um `WARNING` se houver migration pendente — nunca aplica DDL. Isso é
uma rede de segurança: mesmo que o `buildCommand` falhe silenciosamente por
algum motivo, o pior caso é esse warning no log, não uma aplicação de DDL
inesperada numa instância servindo tráfego.

`DATABASE_URL` precisa estar disponível como variável de ambiente **no
build** do serviço `api` (não só em runtime) para `python migrate.py`
conseguir rodar — variáveis de ambiente de Production/Preview do projeto já
ficam disponíveis nas duas fases por padrão na Vercel, mas confirme isso ao
configurar o projeto (ver Environment Variables abaixo).

### ⚠️ Services é beta — valide num Preview antes de promover

O modelo `services` em `vercel.json` (múltiplos serviços num projeto único)
está em beta em todos os planos da Vercel no momento desta migração
(BP-08). Ele substitui o `builds`/`routes` legado que este projeto usava
antes — **teste um deploy de Preview** (PR aberto → Vercel gera preview
automaticamente) e confirme os itens de Pós-deploy abaixo antes de mesclar
para produção. Se o comportamento do Preview divergir do esperado, o
`vercel.json` anterior (`builds`/`routes`, sem build-time migration) está no
histórico do Git — `git show <commit-anterior>:vercel.json` recupera a
versão que funcionava, com `ensure_serverless_schema` verify-only já
resolvendo o problema mais urgente (OPS-01) mesmo sem o `buildCommand`.

### Pré-requisitos
- Banco no Supabase já provisionado (schema aplicado) — `python migrate.py`
  no build cuida de manter o schema em dia a partir daqui.
- **Criar bucket privado** `avatars` no Supabase Storage (Storage → New bucket,
  desmarque "Public").

### Import na Vercel
1. Vercel → Add New → Project → importe o repositório do GitHub.
2. Framework Preset: deixado para o `vercel.json` (modelo `services` — não
   defina um preset manualmente no dashboard). Deixe Root Directory na raiz
   do repo.
3. Environment Variables (aplicam-se a build e runtime dos dois serviços):

| Variável | Valor |
|----------|-------|
| `DATABASE_URL` | **Transaction Pooler** do Supabase (porta **6543**) |
| `JWT_SECRET_KEY` | segredo ≥ 32 chars (`python -c "import secrets;print(secrets.token_hex(32))"`) |
| `ENVIRONMENT` | `production` |
| `ALLOWED_ORIGINS` | vazio (mesma origem) |
| `SUPABASE_URL` | URL do projeto Supabase |
| `SUPABASE_SERVICE_ROLE_KEY` | service role key (nunca `PUBLIC_`) |
| `SUPABASE_AVATARS_BUCKET` | `avatars` |
| `NEXT_PUBLIC_API_BASE_URL` | vazio (mesma origem) |
| OAuth (opcional) | `OAUTH_REDIRECT_BASE_URL=https://<seu-dominio>`, `OAUTH_FRONTEND_CALLBACK_URL=https://<seu-dominio>/oauth/callback`, e as chaves dos provedores |

4. Deploy. `VERCEL=1` é definido automaticamente → conexão-por-request no pooler
   e storage no Supabase.

### Pós-deploy
- Nos logs de build do serviço `api`, confirme que `python migrate.py` rodou
  e terminou sem erro (procure por "Migrações aplicadas com sucesso").
- `GET /api/health/live` → `{"ok": true}` (liveness).
- `GET /api/health` → `{"ok": true, "db": "connected"}` (readiness com DB).
- Cadastro (com aceite) → login → transação → upload de avatar → exportar dados →
  excluir conta.
- Se o roteamento estático do Next apresentar deep-link 404, valide no **Preview**
  antes de promover; fallback: manter o container (Railway/Render/Docker) abaixo.

## Docker (recomendado para desenvolvimento)

```bash
docker compose build
docker compose up
```

- API: `http://localhost:8000`
- Health: `GET /api/health`

## Backend (Python)

```bash
python -m pip install -r requirements.txt
python migrate.py
python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

## Frontend (Next.js)

```bash
cd frontend
npm ci
npm run build
```

Com origens separadas, defina `NEXT_PUBLIC_API_BASE_URL` apontando para a API.

## Variáveis obrigatórias

| Variável | Descrição |
|----------|-----------|
| `DATABASE_URL` | Connection string PostgreSQL / Supabase |
| `JWT_SECRET_KEY` | Segredo com pelo menos 32 caracteres |

## Variáveis recomendadas

| Variável | Descrição |
|----------|-----------|
| `ALLOWED_ORIGINS` | Origens CORS (HTTPS em produção) |
| `ENVIRONMENT` | `development`, `testing` ou `production` |
| `LOG_FORMAT` | `text` ou `json` |

Consulte também [OAuth](oauth.md) para login social.

## Railway

### Pré-requisitos

- Conta no [Railway](https://railway.app)
- Banco PostgreSQL (Supabase ou Postgres do Railway)
- Repositório no GitHub

### Supabase (opcional)

1. Crie o projeto em [supabase.com](https://supabase.com)
2. Settings → Database → Connection string (URI)
3. Use a URI em `DATABASE_URL`

### Deploy

1. Railway → New Project → Deploy from GitHub repo
2. Variáveis:
   - `DATABASE_URL` — connection string
   - `JWT_SECRET_KEY` — gere com `python -c "import secrets; print(secrets.token_hex(32))"`
   - `ENVIRONMENT=production`
   - `ALLOWED_ORIGINS` — URL HTTPS pública (evite `*` após o primeiro deploy)
3. Comando: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Health check: `/api/health`

### Pós-deploy

- `GET /api/health` → `{"ok": true, "db": "connected"}`
- Criar conta de teste, login, transações, logout
- Confirmar HTTPS no navegador

### Manutenção

- `git push` dispara redeploy
- Logs: Railway → Deployments → View Logs

## Render

Use `render.yaml` como referência ou crie Web Service Docker/Python com as mesmas variáveis e health check `/api/health`.

## Checklist de segurança pré-produção

- [ ] `JWT_SECRET_KEY` com ≥ 32 caracteres, só em secrets do provedor
- [ ] `DATABASE_URL` nunca no repositório
- [ ] `.env` no `.gitignore`
- [ ] `ALLOWED_ORIGINS` restrito às origens reais
- [ ] Rate limit ativo em login e cadastro
- [ ] Senhas com bcrypt; `hashed_password` nunca nas respostas
- [ ] Rotas de dados exigem Bearer token e `user_id`
- [ ] Headers de segurança ativos
- [ ] Logs sem tokens, senhas ou PII desnecessária

## Workers e estado

Estado crítico (tokens revogados, imports CSV e endpoints legados de cartao) usa PostgreSQL quando disponível. O app está preparado para múltiplos workers; em escala, considere Redis para blocklist compartilhada.
