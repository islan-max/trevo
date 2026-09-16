# Auditoria técnica do Trevo

*Levantamento conduzido entre 5 e 15 de setembro de 2026. Estado final auditado no commit `b9d368e`, com a produção em `trevo-finance.vercel.app` respondendo saudável e conectada ao banco.*

---

## 1. O que é o sistema

O Trevo é um assistente financeiro pessoal em português, pensado primeiro para o celular, que se propõe a responder uma pergunta bem concreta: quanto eu posso gastar hoje sem furar o mês. Por baixo dessa pergunta simples existe um produto razoavelmente amplo — lançamentos manuais, metas diárias, orçamento por categoria, compras parceladas, cartões com fatura projetada, importação de extrato em CSV, relatórios exportáveis e um índice próprio de saúde financeira que o produto chama de Ritmo Score.

Tecnicamente é um monorepo com duas metades. O backend é uma API FastAPI sobre PostgreSQL, com autenticação por JWT e cinquenta e seis endpoints REST. O frontend é um Next.js 15 exportado como site estático, que consome essa API. A mesma base roda em três alvos de deploy diferentes: Vercel em modo serverless, que é o primário; Docker, onde o próprio FastAPI serve os arquivos estáticos do frontend; e Railway ou Render como alternativas de contêiner. O banco de produção vive num projeto Supabase hospedado em `us-east-2`.

Quando comecei a auditoria, o sistema ainda se chamava Pulsa — ou Pulsar, dependendo de onde se olhasse — e o repositório se chamava `nexo`. A troca de identidade para Trevo aconteceu durante o trabalho e está descrita mais adiante.

---

## 2. A arquitetura, e o descompasso entre o que ela diz ser e o que é

A estrutura de pastas do backend sugere uma aplicação em camadas: existe um `app/core` com configuração, acesso a dados, segurança e armazenamento; um `app/shared` com utilitários de dinheiro e datas; um `app/integrations` com contratos para fontes de dados financeiros; e pastas vazias reservadas para cada domínio — `auth`, `transactions`, `budgets`, `cards`, `goals`, `reports` e assim por diante.

Essa promessa não se cumpria. Praticamente todo o sistema morava num único arquivo, `app/main.py`, com quase cinco mil linhas concentrando rotas, regras de negócio, validação, montagem de consultas SQL, formatação de moeda e até um gerador de PDF escrito à mão, byte a byte, sem biblioteca externa. As pastas de domínio continham apenas um `__init__.py` vazio. O histórico do repositório mostra uma refatoração em fases — extraiu-se configuração, camada de dados, segurança, conformidade com a LGPD e proteção contra CSRF — mas as rotas nunca saíram do monólito. A própria documentação de arquitetura do projeto assumia isso com honestidade, descrevendo o `main.py` como um "adaptador de compatibilidade" durante a transição.

O caso mais revelador desse descompasso era a duplicação silenciosa. Os módulos de `app/shared` e `app/integrations` estavam escritos, testados na prática e **não eram importados por ninguém**. O `main.py` mantinha cópias literais das mesmas funções: conversão e arredondamento de dinheiro, distribuição de parcelas em centavos, cálculo de meses, formatação em real, geração de hash de duplicata, parsing de valores textuais, e até o formatador de log em JSON. Havia, portanto, duas fontes de verdade para as regras de dinheiro do produto, sem nenhum mecanismo que garantisse que continuariam iguais. Era uma divergência esperando acontecer.

Consolidei isso logo no início: o `main.py` passou a importar dos módulos compartilhados e as cópias foram removidas. O arquivo encolheu, e mais importante, as regras monetárias passaram a existir num lugar só. Hoje `app/main.py` tem 5.133 linhas — cresceu de novo ao longo do trabalho, porque a reescrita do fluxo de importação de CSV adicionou código — mas sem a duplicação.

---

## 3. O modelo de dados, que é a parte mais bem resolvida do sistema

A modelagem é o ponto mais sólido do projeto e merece registro. Tudo se pendura na tabela `users`, identificada por UUID, e cada tabela subordinada carrega `user_id` com `ON DELETE CASCADE`. Até aí, convencional.

O que eleva o desenho é o uso de chaves estrangeiras compostas. As transações não referenciam `category_id` e `card_id` isoladamente: referenciam os pares `(user_id, category_id)` e `(user_id, card_id)`. O efeito prático é que se torna fisicamente impossível, no nível do banco, uma transação de um usuário apontar para o cartão ou a categoria de outro — nem por bug de aplicação, nem por consulta mal escrita. Num produto financeiro multiusuário, isso é a diferença entre confiar no código e confiar no banco. É uma decisão madura.

A tabela `transactions` é o centro de gravidade do domínio. Ela carrega tipo, categoria, cartão, mês de cobrança, grupo e número de parcela, recorrência, origem do lançamento — manual, importação de CSV ou o espaço reservado para Open Finance — e um hash de duplicata com índice único parcial, que é o que sustenta a deduplicação na importação.

Uma convenção atravessa quase todas as consultas do sistema: o mês contábil de uma transação é `COALESCE(billing_month, substring(transaction_date from 1 for 7))`. Ou seja, parcelas gravam explicitamente em que fatura caem, e todo o resto assume o mês da própria data. É uma solução simples e eficaz, mas tem um custo que discuto na seção de performance.

---

## 4. As regras de negócio, e onde elas erravam

Três mecanismos concentram a inteligência do produto.

O **ritmo diário** calcula o orçamento disponível como renda mais entradas menos reserva, divide pelos dias do mês para chegar a uma meta diária recomendada, projeta o fechamento a partir da média gasta até hoje e classifica o mês em verde, amarelo ou vermelho conforme a projeção estoure ou não o orçamento. É o coração da tela inicial.

O **Ritmo Score** parte de mil pontos e desconta por cinco eixos: proporção entre gasto e receita, consistência de lançamentos nos últimos três meses, formação de reserva, uso do limite dos cartões e estouro de orçamento. O resultado é limitado entre zero e mil e ganha um rótulo qualitativo.

Os **alertas** varrem o mês em busca de sinais: cartão acima de oitenta por cento do limite, categoria trinta por cento acima da média dos três meses anteriores, saldo projetado negativo, fatura futura alta, meta diária estourada em mais da metade dos dias.

Nesse terreno encontrei um erro de domínio genuíno, do tipo que não aparece em teste automatizado porque ninguém pensou em escrevê-lo. Os cartões têm dia de fechamento cadastrado, mas a criação de parcelas ignorava esse campo por completo: a primeira parcela sempre caía no mês da compra. Na prática, qualquer compra feita depois do fechamento entrava na fatura errada, e todas as parcelas seguintes herdavam o deslocamento. Corrigi introduzindo uma função que decide o primeiro mês de cobrança comparando o dia da compra com o dia de fechamento, incluindo o caso da virada de ano, e cobri o comportamento com testes.

Um segundo ponto que sinalizei mas deixei como está: a simulação de juros em compras parceladas usa uma aproximação de saldo médio, não amortização real. Para valores e prazos maiores, o número mostrado ao usuário diverge do que uma financeira cobraria. Não é um bug de implementação — a fórmula faz o que se propõe — mas é uma imprecisão que o produto deveria assumir explicitamente ou corrigir.

---

## 5. Segurança e privacidade

Essa camada está acima da média para um projeto desse porte, e vale descrever o que já existia antes de eu chegar.

A autenticação usa JWT guardado em cookie `HttpOnly`, com suporte paralelo a `Bearer` para clientes de API. Há proteção CSRF por double-submit, exigida apenas quando a sessão vem por cookie. Tokens podem ser revogados no servidor por hash, e a troca de senha invalida tokens emitidos antes dela comparando o `iat` com o carimbo de `password_changed_at`. O limite de tentativas de login opera em duas camadas: por IP via `slowapi` e por e-mail com contador persistido no Postgres. Quando o e-mail não existe, o sistema ainda executa uma verificação bcrypt contra um hash fictício, para não vazar a existência da conta pelo tempo de resposta.

Os cabeçalhos de segurança estão todos no lugar — CSP, HSTS, `nosniff`, `frame-ancestors`, política de permissões — e as rotas de API respondem com `no-store`. Os logs de auditoria registram e-mail e IP **hasheados**, nunca em texto claro, o que é um cuidado que muitos projetos maiores não têm.

Do lado da LGPD, existe exportação completa dos dados do titular, montada por descoberta no `information_schema` — o que faz novos domínios entrarem automaticamente na exportação — pulando tabelas operacionais e nunca incluindo o hash de senha. Há registro versionado de consentimento e exclusão de conta via cascade.

Encontrei aqui um problema estrutural, não de implementação, que descrevo na seção seguinte porque ele se manifestou como uma falha de infraestrutura.

---

## 6. O diagnóstico de infraestrutura: por que o sistema estava fora do ar

Este foi o achado mais consequente da sessão, e ele desmentiu a premissa inicial.

O pedido original falava em "reativar o banco de dados", sugerindo que o Supabase estivesse pausado por inatividade. Ao inspecionar o painel, o projeto aparecia como **Healthy**, com 27 MB de dados, quinze tabelas e conteúdo real: treze usuários, sessenta e quatro lançamentos e duzentas e dez categorias. O banco nunca esteve desligado.

O problema estava na Vercel. A lista de variáveis de ambiente do projeto continha **uma única entrada**, `ENVIRONMENT`. Faltavam `DATABASE_URL` e `JWT_SECRET_KEY`. O resultado era um sistema no ar e completamente inútil: a API respondia, o frontend carregava, mas o endpoint de saúde retornava `db: error` e qualquer operação que tocasse o banco ou assinasse um token falhava. A aplicação estava viva e vazia.

Investigando o schema, apareceu um segundo problema mais sutil. A tabela `schema_migrations` registrava apenas as migrations de `0001` a `0006`, mas a tabela `consents`, criada pela migration `0008`, **existia no banco**. Havia uma dessincronia entre o schema real e o registro do que fora aplicado. A causa raiz era arquitetural: em modo serverless o startup pula deliberadamente as migrations — uma decisão correta, porque um banco indisponível derrubaria toda a função no cold start — mas o `vercel.json` **não tinha nenhum passo de build que as executasse**. Ou seja, em produção as migrations simplesmente nunca rodavam. O schema estava congelado no que quer que tivesse sido aplicado manualmente em algum momento do passado.

Corrigi isso em três frentes. Primeiro, o `migrate.py` passou a commitar uma migration por transação, em vez de tudo ao final, para que uma falha no meio não descarte o que já foi aplicado. Segundo, introduzi um executor protegido por advisory lock do Postgres, de modo que múltiplas instâncias subindo simultaneamente não apliquem a mesma migration em paralelo. Terceiro, criei um verificador que roda uma vez por processo em serverless, antes da primeira requisição de API, tolerante a falha — se o banco estiver fora, ele registra o erro e serve a requisição mesmo assim, em vez de derrubar a função.

Com a connection string em mãos, apliquei as migrations pendentes — `0008` a `0011` na primeira rodada, `0012` depois — e verifiquei a integridade: dez migrations registradas, dezesseis tabelas, e os treze usuários, sessenta e quatro lançamentos e duzentas e dez categorias preservados, todos com cores válidas da paleta nova.

---

## 7. Deploy e domínio

Três descobertas menores, mas que custavam tempo a quem tentasse entender o sistema.

A URL de produção não era a que se imaginava. O subdomínio `pulsa.vercel.app` estava ocupado por um *create-react-app* de terceiro, sem nenhuma relação com o projeto. O deploy real vivia em `pulsar-khaki.vercel.app`, um nome gerado automaticamente que sobrou de uma renomeação anterior — a Vercel mantém o domínio quando o projeto é renomeado.

Durante a sessão o projeto foi renomeado para `trevo` na Vercel e o repositório para `islan-max/trevo` no GitHub, o que quebrou o remote local até ser atualizado. O domínio de produção foi trocado para `trevo-finance.vercel.app`, mantendo o antigo como redirecionamento 307 em vez de apagá-lo — apagar libera o subdomínio para qualquer pessoa registrar e mata links já compartilhados, e o redirect entrega o mesmo resultado sem esse risco.

Nada no código precisou mudar por causa da troca de domínio, porque `ALLOWED_ORIGINS` e `NEXT_PUBLIC_API_BASE_URL` estão vazios — frontend e API compartilham origem na Vercel, e a configuração acompanha o domínio sozinha.

---

## 8. A troca de identidade para Trevo

O rebrand foi mais do que substituir um nome. Envolveu desenhar uma marca, reconstruir a paleta inteira e trocar a tipografia.

O símbolo é um trevo de quatro folhas construído a partir de um único caminho em forma de coração, girado de noventa em noventa graus, com as folhas alternando entre dois tons do verde da marca — o mesmo princípio de duas tonalidades que depois usei nos ícones. Gerei o SVG para uso na interface e nove PNGs derivados dele para uso externo: símbolo transparente em cinco tamanhos, versão para fundo escuro, duas monocromáticas e um ícone de aplicativo com fundo arredondado.

A paleta partiu do verde da folha como cor de ação, com um dourado discreto como acento de "sorte", e neutros levemente esverdeados. Validei cada par de texto e fundo contra o critério AA da WCAG antes de aplicar, o que pegou dois problemas na origem: o dourado que eu havia escolhido para avisos dava 4,02:1 sobre branco, abaixo do mínimo, e foi escurecido; e a cor de linha era sutil demais para ser percebida como borda.

A migração das cores nos componentes exigiu cuidado com uma armadilha de nomenclatura. O token `leaf` já existia no sistema antigo significando "verde de valor positivo", e no sistema novo `leaf` passaria a significar "cor da marca". Migrar na ordem errada teria colidido os dois significados silenciosamente. Renomeei primeiro o `leaf` antigo para o token semântico `success` e só depois promovi o antigo `pulse` a `leaf` — 213 referências em 67 arquivos, sem colisão.

Nas fontes, Inter para títulos e Nunito para texto corrido, ambas baixadas como arquivos variáveis WOFF2 e servidas pelo próprio domínio. Isso não foi preferência estética: a CSP do backend declara `font-src 'self'`, então fontes carregadas do Google seriam bloqueadas em produção. Self-hosting era obrigatório.

Para os ícones houve uma limitação que precisou de decisão sua. O Font Awesome **Duotone é um produto pago** — o pacote retorna 404 no registry público e exige licença Pro com token de registry. Com sua escolha, usei o Font Awesome Free e construí o efeito duotone com uma técnica que considero superior à simulação ingênua: cada ícone é renderizado com um gradiente de duas paradas derivadas de `currentColor`, o que produz duas tonalidades genuínas no mesmo glifo e faz o ícone herdar automaticamente a cor do contexto, funcionando sobre qualquer fundo e nos dois temas.

---

## 9. Os problemas de interface

Você apontou dois sintomas — o degradê branco nos cards e o excesso de verde no tema escuro — e ambos tinham causa identificável no código.

O **degradê branco** vinha de uma única classe. O card principal do resumo usava um gradiente que passava por `via-ink`, e `ink` é um token que inverte com o tema: escuro no claro, quase branco no escuro. Como o texto do card é sempre branco, no tema escuro o resultado era texto branco sobre fundo quase branco no meio do gradiente. Medindo, o pior ponto do gradiente dava **1,15:1** — praticamente invisível. Substituí por tokens dedicados que permanecem escuros nos dois temas, já que o texto ali é sempre claro. O pior ponto passou a ser 10,5:1.

O **tema escuro excessivamente verde** era responsabilidade minha: a primeira paleta que escrevi tingia fundo, superfícies, bordas e texto secundário com verde, o que cansava a leitura. Redesenhei para uma base de cinzas levemente frios, mantendo o verde apenas como acento em elementos de ação e estado. Validei os treze pares principais de contraste antes de aplicar.

Nessa revisão apareceu um terceiro problema, que eu não estava procurando e que era **pior que os dois anteriores**. A combinação `bg-leaf` com texto branco pintava todo estado ativo do produto — item de navegação selecionado, passo atual do importador, opção escolhida em listas suspensas, avatar do usuário. Esse par dá **3,45:1 no tema claro e 2,48:1 no escuro**. Os dois reprovam em AA. O elemento selecionado era, portanto, o menos legível da interface inteira, nos dois temas. Corrigi com um par de tokens escolhido por tema — verde escuro com texto branco no claro, verde claro com texto escuro no escuro — chegando a 4,95:1 e 7,79:1.

Também corrigi a paleta dos gráficos, que ainda trazia roxo e rosa herdados da marca anterior e um teal como cor de fallback. Passaram a sair de um módulo único, com matizes distinguíveis entre si, luminosidade próxima para nenhuma série dominar por brilho, e evitando depender do par vermelho/verde, que é a forma mais comum de daltonismo. De quebra, o eixo dos gráficos dividia todos os valores por mil independentemente da escala, transformando R$ 240 em "R$0.24k"; agora só usa milhares quando os valores justificam.

Por fim, o estado de carregamento. A tela mostrava "Seu resumo está carregando" sobre valores em R$ 0,00 — indistinguível de uma conta genuinamente vazia. Substituí por esqueletos com `aria-busy`, que comunicam ausência de dado em vez de fingir um zero.

---

## 10. Uma correção da própria auditoria

Um registro de honestidade que considero parte do trabalho.

Na primeira versão do relatório, apontei que faltavam *skip link*, `aria-current` e marcos semânticos de navegação. Ao reinspecionar o código para implementar a correção, descobri que **os três já existiam** — no `AppShell` e no `BottomNav` — e que os alvos de toque da navegação já tinham 56 pixels, acima do mínimo recomendado. Minha avaliação estava errada.

A reinspeção, porém, revelou no mesmo terreno o problema de contraste do estado ativo descrito acima, que era real e mais grave do que o que eu havia reportado por engano. A ficha correspondente no relatório foi reescrita para registrar o erro e a descoberta que o substituiu.

---

## 11. Performance

O Lighthouse apontava 91 de performance com LCP de 3,5 segundos e cerca de 250 KiB de JavaScript não utilizado. A causa dominante era única e localizável: a biblioteca de gráficos `recharts` era importada estaticamente pelo componente do resumo, que é a primeira tela depois do login. Isso colocava a biblioteca inteira no bundle inicial mesmo para quem não rolasse até os gráficos.

Extraí os dois gráficos para módulos próprios e passei a carregá-los sob demanda, com renderização apenas no cliente — a biblioteca mede o contêiner para se dimensionar, então não faz sentido no servidor. Envolver os componentes individualmente não funcionaria, porque a biblioteca inspeciona seus filhos e wrappers dinâmicos quebram essa introspecção; era necessário extrair os blocos inteiros. Cada gráfico ganhou um esqueleto com a altura exata do gráfico final, para que o carregamento tardio não empurre o conteúdo abaixo e gere deslocamento de layout.

O resultado medido: a primeira tela caiu de **244 kB para 130 kB** de JavaScript, uma redução de 47%.

No backend, a análise encontrou um padrão de N+1 espalhado. A montagem do dashboard executava doze consultas em laço só para desenhar a série mensal, e a geração de alertas fazia uma consulta por categoria do usuário. Pior: o endpoint de bootstrap, que a tela inicial chama, pedia o resumo de orçamento três vezes e as metas duas vezes dentro da mesma requisição, porque funções diferentes recalculavam as mesmas agregações. Consolidei as consultas em laço em agregações únicas e introduzi um cache com escopo de requisição, que vive apenas enquanto a requisição dura e portanto nunca serve dado velho entre requisições.

No banco, os índices existentes cobriam o filtro por usuário e mês, mas o Postgres ainda precisava visitar a tabela para ler `amount` e `type` — justamente as colunas que toda agregação soma. Criei um índice de expressão que inclui essas colunas, o que transforma as agregações em varredura apenas de índice. A expressão precisa bater exatamente com a usada nas consultas, senão o planejador a ignora; confirmei com `EXPLAIN (ANALYZE, BUFFERS)` que o índice novo é de fato escolhido.

Fica pendente reexecutar o Lighthouse contra o deploy atual. O bundle caiu quase pela metade, mas **não vou afirmar um LCP que não medi** — os números de LCP e da nota final permanecem em aberto no relatório até que a auditoria seja repetida.

---

## 12. Qualidade, testes e o incidente que causei

A suíte tinha um problema silencioso: os testes de integração pulam quando `TEST_DATABASE_URL` não está definida. Localmente isso significava ver a suíte verde sem ter executado vinte e oito testes. Não é um defeito em si — é um mecanismo razoável — mas comunicava a coisa errada. Fiz o `conftest` imprimir um aviso destacado no cabeçalho quando roda sem banco, e adicionei um passo de CI que falha o build se qualquer teste for pulado, já que lá o Postgres está disponível e um skip significa configuração quebrada.

O lint também estava frágil. O projeto declarava `ruff>=0.4` sem configuração própria, e versões novas da ferramenta trazem regras novas habilitadas por padrão. Na prática, o CI quebraria sozinho em alguma atualização — e de fato apontava mais de duzentos problemas com a versão atual. Escrevi uma configuração explícita, fixando o conjunto de regras e documentando os dois descartes: as dependências declaradas como valor padrão de parâmetro, que são o idioma do FastAPI e não o antipadrão que a regra procura, e a validação de datas sem fuso, que aqui são datas puras onde fuso não significaria nada. Corrigi os problemas reais que sobraram, incluindo o encadeamento de exceções para não perder a causa raiz.

E houve um incidente operacional que **eu causei**, e que merece registro completo porque a lição é boa.

Criei um módulo novo chamado `app/core/secrets.py`. O `.gitignore` do projeto tem uma regra `secrets.*`, cuja função é impedir que arquivos de segredo sejam versionados — uma regra correta, que deve permanecer. Ela casou com o meu arquivo. O `git add -A` o ignorou **em silêncio**, sem aviso, e eu não conferi o que havia sido preparado antes de commitar. O deploy subiu sem o arquivo e a produção inteira caiu: todas as rotas retornando 500, inclusive a de liveness, com `ModuleNotFoundError` no import. Ficou assim por cerca de seis minutos, até eu localizar a causa nos logs de runtime da Vercel.

A correção foi renomear o módulo para `app/core/signing.py`, que além de não colidir descreve melhor o que ele faz, e manter a regra do `.gitignore` intacta. Mas a correção estrutural foi outra: escrevi um verificador que detecta arquivos de código sendo silenciosamente ignorados pelo `.gitignore` e o coloquei no CI. Testei recriando exatamente o arquivo do incidente — o verificador acusa e falha o build.

---

## 13. O que não foi possível fazer, e por quê

Quatro limites reais apareceram durante o trabalho, e nenhum deles é técnico no sentido de "faltou tentar".

**As credenciais dos provedores de OAuth.** O login social não funciona porque as variáveis dos provedores não existem no ambiente — o endpoint de status reporta `configured: false` e `redirect_ready: false` para Google, GitHub e Facebook. O código está implementado e tem testes; inclusive o estado do fluxo OAuth foi corrigido antes para ser assinado e sem estado no servidor, o que é o que permite que autorização e callback sejam atendidos por invocações serverless diferentes. O que falta é registrar as aplicações nos provedores e cadastrar as chaves — trabalho que exige criar contas em serviços de terceiros e manusear segredos, e que não me cabe fazer.

**O cadastro das variáveis de ambiente.** Pelo mesmo motivo, não pude colar a connection string nem o segredo de assinatura nos formulários da Vercel. Tentei o caminho que contornaria isso sem eu tocar na credencial — a integração nativa entre Supabase e Vercel, que passa os valores diretamente entre as plataformas — mas ela falhou do lado da Vercel com "The installation could not be started" em duas tentativas, e do lado do Supabase essa integração não existe mais. Reduzi o problema pela metade por outro caminho: fiz o servidor provisionar seu próprio segredo de assinatura quando a variável não existe, gerando-o uma vez e persistindo-o na tabela `app_secrets`. Persistir é essencial — gerar por processo invalidaria todas as sessões a cada cold start. Com isso restou uma única variável para você cadastrar.

**O Font Awesome Duotone**, por ser produto pago, conforme já descrito.

**O Open Finance.** Esse merece franqueza. A implementação atual é um espaço reservado: existe um contrato de interface em `app/integrations`, um valor `open_finance_future` aceito na coluna de origem das transações, e nada mais. Não há autenticação, consentimento, certificados nem chamadas a instituição alguma. E a razão pela qual isso não é um item de backlog comum é regulatória: no Brasil, consumir as APIs de Open Finance exige ser instituição autorizada pelo Banco Central, com certificados ICP-Brasil, registro no diretório de participantes e conformidade com um regime de segurança específico. O caminho viável para um produto neste estágio é intermediar por um agregador licenciado — Pluggy ou Belvo, por exemplo — o que muda a natureza do trabalho de "implementar um protocolo" para "integrar um fornecedor e modelar consentimento, sincronização e expiração". A documentação do próprio projeto já era honesta nisso, dizendo que não há conexão bancária real hoje, e essa honestidade deve ser mantida na interface.

---

## 14. Conclusão e prioridades

O Trevo é um sistema com fundamentos melhores do que sua aparência sugeria. A modelagem de dados é sólida, com garantias de isolamento entre usuários no nível do banco. A camada de segurança é cuidadosa e cobre coisas que projetos maiores esquecem. O domínio financeiro é coerente e os cálculos usam decimal com distribuição exata de centavos.

Os problemas que encontrei se dividem em três naturezas distintas. Havia **falhas de configuração de infraestrutura** que deixavam o sistema no ar e inoperante, e essas estão resolvidas — o banco está conectado, migrado e validado. Havia **defeitos concretos** de contraste, performance, domínio e robustez, e a maioria foi corrigida e está em produção. E há uma **dívida arquitetural** — o monólito de cinco mil linhas — que não é uma falha a corrigir num dia, mas uma evolução a conduzir em etapas, começando pela extração das rotas em roteadores por domínio, aproveitando as pastas que já existem vazias justamente para isso.

Em ordem de prioridade, o que eu faria a seguir: registrar as aplicações nos provedores para destravar o login social, que é meia hora de trabalho e desbloqueia um recurso inteiro; rotacionar a senha do banco, que passou por este chat e deve ser considerada exposta; reexecutar o Lighthouse para fechar as métricas que deixei em aberto; aplicar o carregamento sob demanda também na página de relatórios, que ainda carrega 240 kB; e então começar a extração dos roteadores, que é o passo que torna todo o resto mais fácil de manter.

---

*Este documento descreve o estado no commit `b9d368e`. As afirmações sobre correções aplicadas foram verificadas contra a produção; as que dependem de medição futura estão marcadas como tal no texto.*
