-- SOMENTE LEITURA. Lista grupos de parcelas com installment_number
-- duplicado — sinal de que duas compras distintas colidiram no mesmo
-- installment_group antes da correção de DOM-01 (que passou a gerar um UUID
-- por compra em vez de uma chave derivada de user_id+card_id+título+data).
--
-- Este script não corrige nada. Se ele devolver linhas, cada uma precisa de
-- decisão manual: as duplicatas podem ser duas compras genuinamente
-- distintas que colidiram (uma delas precisa de installment_group novo) ou
-- o resultado de uma exclusão que já apagou o par errado.
--
-- Uso: psql "$DATABASE_URL" -f scripts/check-installment-groups.sql

SELECT
  user_id,
  installment_group,
  installment_number,
  COUNT(*) AS ocorrencias,
  array_agg(id ORDER BY id) AS transaction_ids,
  array_agg(title ORDER BY id) AS titles,
  array_agg(transaction_date ORDER BY id) AS transaction_dates
FROM transactions
WHERE installment_group IS NOT NULL
GROUP BY user_id, installment_group, installment_number
HAVING COUNT(*) > 1
ORDER BY user_id, installment_group, installment_number;
