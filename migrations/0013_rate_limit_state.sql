-- Rate limit por IP persistido no Postgres.
--
-- slowapi (Limiter(key_func=get_remote_address)) usa MemoryStorage por padrão
-- — verificado em runtime. Em serverless cada instância tem o próprio
-- contador, então o limite por IP em rotas como /api/auth/register,
-- /api/auth/change-password e /api/privacy/export é, na prática, decorativo.
--
-- A tabela segue o mesmo desenho de login_failures_state (migration 0005):
-- janela deslizante simples por identificador, sem TTL automático — a limpeza
-- acontece por leitura (uma janela expirada é tratada como se não existisse).

CREATE TABLE IF NOT EXISTS rate_limit_state (
  key_hash TEXT PRIMARY KEY,
  window_start TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_rate_limit_state_window_start ON rate_limit_state(window_start);
