-- Coluna dedicada para a conta/origem informada num extrato importado.
--
-- CSV-08: confirm_csv_import gravava a conta (quando a coluna existia no
-- arquivo) em payment_method, misturando dois conceitos diferentes —
-- "forma de pagamento" (pix, débito, crédito) e "conta de origem" (Nubank,
-- Conta Corrente) — e poluindo o paymentMethodBreakdown do dashboard com
-- nomes de conta em vez de forma de pagamento.

ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS account TEXT;
