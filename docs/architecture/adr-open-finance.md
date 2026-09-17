# ADR: Open Finance — agregador licenciado, e se/quando integrar

- **Status:** ADIADO (postergado), com data de reavaliação em **2027-03-17**
  ou ao primeiro sinal de receita recorrente (o que vier primeiro — ver
  [Decisão](#decisão)).
- **Contexto:** [BP-12](../auditoria-2026-09.md) da auditoria técnica de
  2026-09. Fase 1 do prompt — só decisão e modelagem no papel, nenhum código
  de integração.
- **Data:** 2026-09-17.

## Contexto

O código já tem um espaço reservado honesto para isso:
`app/integrations/base.py` define o contrato (`FinancialDataSource`,
`ImportedTransaction`) que uma integração real implementaria, e
`app/integrations/normalizer.py` já tem o pipeline de deduplicação/
normalização que a importação de CSV usa hoje. Nada disso está conectado a
nenhum provedor — é desenho, não código vivo (ver docstring de
`app/integrations/base.py`, corrigida no BP-10).

No Brasil, falar diretamente com o Open Finance regulado exige ser
instituição autorizada pelo Banco Central, com certificado ICP-Brasil e
registro no diretório de participantes. Isso está fora de cogitação para um
projeto neste estágio — o único caminho viável é um **agregador licenciado**
que já é Iniciador de Transação de Pagamento (ITP) registrado, e que
devolve só os dados finais depois de conduzir ele mesmo o fluxo de conexão
e consentimento com o banco.

## 1. Comparação Pluggy × Belvo

Números de mercado (preço) não são publicados oficialmente por nenhum dos
dois — os dois vendem sob consulta. Os valores abaixo vêm de um
[relato público de outro desenvolvedor de app de finanças pessoais
enfrentando exatamente esta decisão](https://www.tabnews.com.br/GuilhermeVieira/estou-desenvolvendo-um-app-de-financas-pessoais-e-nao-consigo-pagar-o-open-finance-pluggy-r2-5k-mes-belvo-r6k-mes-tecnospeed-r1-5k-de-entrada-r540)
— tratem como uma referência de ordem de grandeza, não uma cotação
vinculante; **confirmar direto com cada fornecedor antes de qualquer
decisão financeira**.

| Critério | Pluggy | Belvo |
|---|---|---|
| Cobertura no Brasil | [130+ instituições](https://www.pluggy.ai/produtos/open-finance), incluindo os 5 grandes bancos e os digitais de peso (Nubank, Inter, C6) | [60+ instituições](https://finsidersbrasil.com.br/reportagem-exclusiva-fintechs/belvo-passa-a-oferecer-servico-de-iniciacao-de-pagamento/) diretamente no Open Finance brasileiro; a marca "90% das contas da América Latina" é regional, não específica do Brasil |
| Piso de custo mensal (relato de mercado) | ~R$ 2.500/mês | ~R$ 6.000/mês |
| Licença regulatória | ITP autorizada pelo Banco Central | ITP autorizada pelo Banco Central (desde set/2022) |
| Modelo de consentimento | Conduz o fluxo de conexão e consentimento com o banco por conta própria; devolve só dados finais | Mesmo modelo — participante registrado, conduz consentimento |
| Onboarding técnico | SDK + widget prontos; relatos de times de produto integrando o primeiro fluxo em 1–5 dias úteis | Comparável — API + agregação documentada |
| O que sobra para o integrador (Trevo) | Modelar `connections`/`sync_runs`, política de expiração/revogação, reconciliação e UX de consentimento — a parte regulatória pesada (certificado, registro, diretório de participantes) fica com o agregador | Mesma divisão de responsabilidade |

**Correção a uma premissa da auditoria:** o prompt do BP-12 presumia
expiração de consentimento "tipicamente 12 meses". A **Resolução Conjunta
CMN/BC nº 7/2023** já removeu esse teto fixo — instituições podem oferecer
prazos mais longos, com renovação simplificada (sem repetir todo o fluxo de
consentimento) e revogação sempre disponível a qualquer momento, por
iniciativa exclusiva do titular dos dados. Projetar `consent_expires_at`
como campo nulável (ver modelagem abaixo) em vez de assumir 12 meses fixos.

**Se a decisão futura for GO:** Pluggy é a recomendação, por cobertura
maior e piso de custo menor no relato de mercado encontrado — mas
**cotar os dois de novo na data da reavaliação**, já que preço e cobertura
mudam.

## 2. Modelagem de domínio (papel — nenhuma migration criada nesta fase)

Duas tabelas novas, seguindo o padrão de isolamento multiusuário que já
existe em todo o resto do schema (FK composta `(user_id, id)` — ver
[docs/architecture/database.md](database.md)):

```sql
-- Uma linha por vínculo usuário↔instituição↔provedor. "provider" existe
-- para não amarrar o histórico a um único agregador se a decisão mudar.
CREATE TABLE connections (
    id                      SERIAL,
    user_id                 UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider                TEXT NOT NULL,              -- 'pluggy' | 'belvo'
    external_connection_id  TEXT NOT NULL,               -- item_id (Pluggy) / link_id (Belvo)
    institution_id          TEXT NOT NULL,               -- id da instituição no provedor
    institution_name        TEXT NOT NULL,               -- nome legível, para a UI
    status                  TEXT NOT NULL,               -- 'active' | 'expired' | 'revoked' | 'error' | 'pending_mfa'
    consent_granted_at      TIMESTAMPTZ NOT NULL,
    consent_expires_at      TIMESTAMPTZ,                 -- NULL permitido (Res. Conjunta 7/2023 acabou com teto de 12 meses)
    revoked_at              TIMESTAMPTZ,
    last_synced_at          TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, id),
    UNIQUE (user_id, provider, external_connection_id)
);

-- Um lote por sincronização sob demanda (nunca agendada — ver Restrições).
CREATE TABLE sync_runs (
    id                       SERIAL,
    user_id                  UUID NOT NULL,
    connection_id            INTEGER NOT NULL,
    started_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at              TIMESTAMPTZ,
    window_start             DATE NOT NULL,
    window_end               DATE NOT NULL,
    status                   TEXT NOT NULL,              -- 'running' | 'success' | 'partial' | 'failed'
    transactions_fetched     INTEGER NOT NULL DEFAULT 0,
    transactions_imported    INTEGER NOT NULL DEFAULT 0,
    transactions_duplicated  INTEGER NOT NULL DEFAULT 0,
    error_message            TEXT,
    PRIMARY KEY (user_id, id),
    FOREIGN KEY (user_id, connection_id) REFERENCES connections (user_id, id)
);
```

Reaproveitamento do que já existe (nada disso é novo):

- `transactions.import_batch_id` passa a apontar para `sync_runs.id` do
  mesmo jeito que hoje aponta para um lote de importação de CSV — mesma
  coluna, nova origem.
- `transactions.duplicate_hash` (3 níveis, já implementado em
  `app/integrations/normalizer.py::build_duplicate_hash`) roda igual para
  transações vindas do banco — é o pipeline que faz `ImportedTransaction`
  virar linha em `transactions` sem duplicar o que o usuário já lançou à
  mão ou importou por CSV.
- `transactions.source = 'open_finance_future'` já existe na constraint
  (ver `app/integrations/open_finance/README.md`) — só passa a ser
  realmente gravado quando isto sair do papel.

## 3. Política de expiração e revogação de consentimento

- **Expiração:** sem teto regulatório fixo (ver correção acima). Guardar o
  `consent_expires_at` que o próprio provedor devolver; se ele não devolver
  nenhum, `connections.status` vira `'expired'` só quando uma sincronização
  falhar por consentimento inválido — nunca por uma data que o Trevo
  inventou.
- **Renovação:** o fluxo simplificado da Resolução 7/2023 (usuário só
  confirma, sem repetir o consentimento inteiro) é responsabilidade do
  provedor — o Trevo apenas reabre o widget/SDK dele quando
  `status = 'expiring_soon'` ou `'expired'`.
- **Revogação:** só o titular revoga, e só pelo canal do próprio provedor
  (nunca um botão "desconectar" que só apaga a linha local sem avisar o
  banco — isso deixaria o consentimento ativo no lado do banco enquanto o
  Trevo já esqueceu da conexão). Ao receber a confirmação de revogação:
  `connections.status = 'revoked'`, `revoked_at = now()`.
- **Dados já importados na revogação:** **não apagar.** Revogar o
  consentimento futuro não é uma solicitação de exclusão de dados (essa já
  existe, separada — `DELETE /api/auth/me`/exportação LGPD, ver
  [docs/security/security.md](../security/security.md)). As transações já
  trazidas continuam como histórico do usuário, só que a partir daquele
  momento sem nenhuma sincronização nova — igual ao que já acontece hoje
  quando alguém para de importar CSV de um banco.

## 4. Política de reconciliação

| Cenário | Comportamento |
|---|---|
| Transação pendente vira consolidada | Guardar `external_id` (já existe em `ImportedTransaction`) e o status bruto do provedor. Em cada sincronização, dar `UPDATE` na linha existente por `external_id` em vez de inserir de novo — nunca duplicar uma transação que já mudou de status. |
| Transação some do extrato | **Nunca apagar silenciosamente** um registro financeiro que o usuário já viu — diferente de um `SELECT` que só não trouxe a linha, sumir do extrato pode ser estorno do banco ou instabilidade momentânea do provedor. Marcar como `stale` depois de N sincronizações consecutivas sem reaparecer (número exato a decidir na Fase 2, com dado real de quão instável cada provedor é na prática) e sinalizar para revisão do usuário — nunca deletar automaticamente. |
| Transação muda de valor | Acontece com gorjeta, conversão de moeda ou ajuste do lançamento original. `UPDATE` por `external_id`, e registrar em log de auditoria o valor antigo e o novo (mesmo padrão de `audit_log` que já existe em `app/core/logging.py`) — o usuário não pode ser surpreendido por um valor que mudou sem explicação. |

## 5. Revisão de copy do produto

Verificado: nenhuma tela do produto promete conexão bancária real hoje.
`README.md` e `docs/product/vision.md` já são honestos sobre isso; o filtro
"Open Finance futuro" em `frontend/app/transacoes/page.tsx` já rotula a
opção como futura, não como recurso disponível. A única correção necessária
era técnica, não de produto: `docs/product/open-finance.md` citava uma
classe `TransactionNormalizer` que nunca existiu no código (o mesmo erro do
DOC-01 do BP-10, num arquivo diferente) — corrigida neste commit para
apontar para o pipeline real (`app/integrations/normalizer.py`,
`app/imports/service.py`).

## Decisão

**ADIAR.** Não é NO-GO permanente — é "ainda não faz sentido no estágio
atual do produto".

**Por quê:** mesmo o piso mais barato encontrado (~R$ 2.500/mês, Pluggy,
não confirmado oficialmente) é um custo fixo mensal independente do número
de usuários. Para um produto neste estágio, sem uma base de usuários
pagantes que sustente esse custo fixo, a integração inviabiliza o
orçamento antes de gerar qualquer receita — exatamente o risco que a
própria auditoria técnica apontou antes de qualquer código ser escrito.

**Gatilho de reavaliação (o que vier primeiro):**

1. Data fixa: **2027-03-17** (6 meses a partir desta decisão) — cotar os
   dois fornecedores de novo, porque preço e cobertura mudam.
2. Sinal de receita recorrente que sustente um custo fixo mensal de R$
   2.500–6.000 sem comprometer a operação do resto do produto.
3. Validação explícita de usuários dispostos a pagar por importação
   bancária automática (hoje ninguém pediu isso; CSV manual continua sendo
   o único caminho real).

Quando qualquer um desses acontecer, reabrir este ADR, recotar Pluggy e
Belvo com números atualizados, e só então iniciar a Fase 2 (código de
integração) descrita no prompt do BP-12.

## Consequências

- Nenhuma dependência nova adicionada nesta fase (critério de aceite do
  BP-12).
- `app/integrations/base.py` continua sendo contrato de desenho, sem
  nenhum caminho de código chamando-o — isso é o esperado até a Fase 2.
- A modelagem de `connections`/`sync_runs` acima já pode ser revisada por
  quem for implementar a Fase 2 no futuro, sem precisar redesenhar do zero.
