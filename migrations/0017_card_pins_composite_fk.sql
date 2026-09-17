-- DB-01: card_pins referenciava cards(id) e users(id) separadamente — a
-- única tabela do projeto sem a FK composta (user_id, id) que toda outra
-- tabela usa para garantir isolamento por usuário. Sem ela, um card_pins com
-- user_id de UM usuário e card_id de OUTRO nunca era rejeitado pelo banco.
--
-- Verificação prévia: para antes de alterar qualquer coisa se já existir uma
-- linha inconsistente (não corrige dado automaticamente — ver REGRA DE
-- ESCOPO do BP-08).
DO $$
DECLARE
  inconsistent_count INTEGER;
BEGIN
  SELECT COUNT(*) INTO inconsistent_count
  FROM card_pins
  JOIN cards ON cards.id = card_pins.card_id
  WHERE cards.user_id <> card_pins.user_id;

  IF inconsistent_count > 0 THEN
    RAISE EXCEPTION
      'card_pins tem % linha(s) cujo user_id não bate com o dono do cartão (cards.user_id) — corrija os dados antes de aplicar esta migration.',
      inconsistent_count;
  END IF;
END $$;

ALTER TABLE card_pins DROP CONSTRAINT IF EXISTS card_pins_card_id_fkey;

ALTER TABLE card_pins
  ADD CONSTRAINT card_pins_user_card_fkey
  FOREIGN KEY (user_id, card_id) REFERENCES cards(user_id, id) ON DELETE CASCADE;
