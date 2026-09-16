-- Registro de falhas ao remover o avatar de uma conta já excluída (SEC-11).
--
-- storage.remove_avatar() era best-effort e engolia a exceção: se o Supabase
-- falhasse durante a exclusão de conta (LGPD Art. 18, IV), o arquivo ficava
-- órfão no bucket sem nenhum registro — a eliminação ficava incompleta e
-- ninguém saberia. user_id é só referência (a conta já foi apagada quando
-- esta tabela é usada) e por isso NÃO tem FK para users — uma FK com CASCADE
-- apagaria a própria pendência que este registro existe para rastrear.

CREATE TABLE IF NOT EXISTS pending_avatar_deletions (
  id BIGSERIAL PRIMARY KEY,
  user_id UUID,
  avatar_ref TEXT NOT NULL,
  failed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_pending_avatar_deletions_unresolved
  ON pending_avatar_deletions(failed_at)
  WHERE resolved_at IS NULL;
