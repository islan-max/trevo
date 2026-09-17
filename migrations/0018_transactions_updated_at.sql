-- DB-05: users, categories e budgets têm updated_at com trigger
-- set_updated_at (definida em 0000_baseline.sql); transactions não tinha,
-- então não havia como saber se um lançamento foi editado depois de criado.
ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

DROP TRIGGER IF EXISTS transactions_set_updated_at ON transactions;
CREATE TRIGGER transactions_set_updated_at
BEFORE UPDATE ON transactions
FOR EACH ROW
EXECUTE FUNCTION set_updated_at();
