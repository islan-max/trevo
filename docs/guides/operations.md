# Operations

## Deploy

Railway:
1. Configure `DATABASE_URL`, `JWT_SECRET_KEY`, `ENVIRONMENT=production` e `ALLOWED_ORIGINS` com a URL HTTPS publica.
2. Rode `python migrate.py` uma vez ou deixe o startup aplicar migrations.
3. Use o comando `uvicorn main:app --host 0.0.0.0 --port $PORT`.

Render:
1. Crie um Web Service Python ou Docker.
2. Configure as mesmas variaveis obrigatorias.
3. Health check: `/api/health`.

## Desenvolvimento Local Com Docker

```bash
docker compose up
```

A API fica em `http://localhost:8000`.

## Testes

```bash
make test
make test-unit
make test-integration
```

Para integracao, defina `TEST_DATABASE_URL`.

## Migrations

```bash
python migrate.py
```

## Variaveis Obrigatorias

- `DATABASE_URL`: Postgres/Supabase connection string.
- `JWT_SECRET_KEY`: segredo com pelo menos 32 caracteres.
- `ALLOWED_ORIGINS`: origens permitidas para CORS em producao.
- `ENVIRONMENT`: `development`, `testing` ou `production`.
- `LOG_FORMAT`: `text` ou `json`.

## Logs JSON

Em producao:

```bash
LOG_FORMAT=json uvicorn main:app --host 0.0.0.0 --port 8000
```

Os logs incluem `timestamp`, `level`, `service`, `request_id` e `message`.

## Runbook

DB fora do ar:
- Verifique `/api/health`.
- Confirme `DATABASE_URL` e conectividade com o Postgres.
- O pool reconecta no proximo startup; reinicie o servico se o provedor derrubou conexoes antigas.

429 Too Many Requests:
- Login: 5 tentativas a cada 15 minutos.
- Cadastro: 3 tentativas por hora.
- Troca de senha: 3 tentativas por hora.
- Export CSV/PDF: 20 requisicoes por hora.
- Endpoints legados de cartao: bloqueio por PIN continua ativo para compatibilidade.

Token invalido:
- Peça para o usuario sair e entrar novamente.
- Em multiplos workers, use Redis para compartilhar a blocklist de tokens revogados.

Avatar orfao apos exclusao de conta (SEC-11):
- `storage.remove_avatar()` e melhor-esforco; se falhar durante
  `DELETE /api/auth/me`, a conta ja foi apagada e a falha fica registrada em
  `pending_avatar_deletions` (sem FK para `users`, de proposito — o usuario
  ja nao existe).
- Verificar pendencias: `SELECT * FROM pending_avatar_deletions WHERE resolved_at IS NULL ORDER BY failed_at;`
- Para cada linha, remover manualmente o objeto (`avatar_ref` comeca com
  `supabase://` para o bucket privado, ou `/media/profile-photos/` para
  disco local) e marcar como resolvido:
  `UPDATE pending_avatar_deletions SET resolved_at = NOW() WHERE id = %s;`

## Monitoramento

Monitore `GET /api/health`. SLA sugerido: 99,5% mensal para o app e latencia de health check abaixo de 500 ms em condicoes normais.
