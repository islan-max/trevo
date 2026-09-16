-- Rastreia qual lote de importação de CSV gerou cada lançamento.
--
-- Sem isto não havia como distinguir "os lançamentos que este arquivo trouxe"
-- de "todos os lançamentos importados por CSV no mês" — a distinção que o
-- modo "substituir" da importação (DATA-01) e um futuro "desfazer última
-- importação" precisam.

ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS import_batch_id UUID;

CREATE INDEX IF NOT EXISTS idx_transactions_user_import_batch
  ON transactions(user_id, import_batch_id)
  WHERE import_batch_id IS NOT NULL;
