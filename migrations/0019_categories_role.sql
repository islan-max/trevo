-- FIN-03: calculate_score identificava reserva/investimentos por
-- `lower(c.name) IN ('reserva', 'investimentos')` — renomear a categoria
-- padrão zerava silenciosamente esse eixo do Ritmo Score. `role` marca a
-- função da categoria de forma estável, independente do nome escolhido pelo
-- usuário depois.
ALTER TABLE categories
  ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'other';

ALTER TABLE categories
  DROP CONSTRAINT IF EXISTS categories_role_check;

ALTER TABLE categories
  ADD CONSTRAINT categories_role_check
  CHECK (role IN ('reserve', 'investment', 'income', 'other'));

-- Backfill só nas categorias padrão (is_default = 1): são as únicas cujo
-- nome e finalidade são conhecidos de antemão. Categorias criadas pelo
-- usuário ficam com o default 'other' — sem heurística de nome sobre dado
-- que o usuário escreveu.
UPDATE categories SET role = 'reserve' WHERE is_default = 1 AND lower(name) = 'reserva';
UPDATE categories SET role = 'investment' WHERE is_default = 1 AND lower(name) = 'investimentos';
UPDATE categories SET role = 'income' WHERE is_default = 1 AND lower(name) IN ('salário', 'freelance');
