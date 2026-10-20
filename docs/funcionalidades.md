# Funcionalidades e contratos do DocuFlow

Este guia reúne detalhes de API, permissões, estados, extração e integração demonstrativa conferidos no código atual. Para iniciar localmente, consulte [desenvolvimento local](desenvolvimento-local.md); para validar, consulte [testes](testes.md).

## API HTTP

Para iniciar a aplicação, siga [o guia de desenvolvimento local](desenvolvimento-local.md).

`GET /health` retorna `{"status":"ok"}`. `POST /auth/token` recebe formulário OAuth2 com `username` (e-mail) e `password`, e retorna um token Bearer. `GET /auth/me` exige esse token e lista o usuário, workspaces e papéis disponíveis. `POST /documents/analyze` também exige `Authorization: Bearer <token>` e o cabeçalho `X-Workspace-ID`. O formulário multipart contém `file` (PDF, PNG ou JPEG) e `purchase_order_reference` (por exemplo, `PC-2026-0001`). O backend confirma o vínculo do usuário ao workspace, exige o papel `operator` e só então busca o pedido naquele workspace. O repositório aplica o filtro de workspace na consulta.

`GET /dashboard?start_date=AAAA-MM-DD&end_date=AAAA-MM-DD` exige token e `X-Workspace-ID` de um workspace ao qual o usuário pertence. O período é inclusivo e limitado a 366 dias. As consultas retornam somente contagens: documentos aguardando processamento e falhas representam o estado atual; “aguardando revisão” significa processamento pronto, revisão pendente e nenhuma correção salva; “aguardando aprovação” significa processamento pronto, revisão pendente e uma correção salva. Esses dois últimos grupos não se sobrepõem. Envios ao ERP, volume diário e divergências são limitados ao período: envios `queued` e `processing` compõem “pendentes”; volume usa a data de envio do documento; divergências contam códigos com severidade `divergence` na validação persistida e mostram até cinco tipos mais frequentes. O endpoint executa agregações no banco, filtra cada consulta pelo workspace e não devolve nomes, arquivos ou valores monetários. O painel exibe uma nota de que os indicadores são operacionais, não métricas financeiras certificadas.

`POST /documents/analyze` valida tipo e tamanho, grava o registro e o arquivo privado e só então enfileira o job. A resposta inicial é HTTP 202 com `document_id` e `state: queued`. `review_status` continua independente e começa como `pending`; nenhum documento é aprovado automaticamente. Reenviar bytes com o mesmo SHA-256 no mesmo workspace retorna HTTP 200 com `duplicate: true` e o ID já registrado. O mesmo arquivo pode ser registrado em outro workspace.

Um usuário com papel `operator` pode atribuir ou remover o responsável de qualquer documento do workspace por `PUT /documents/{id}/assignment`, enviando `{"assignee_user_id": <id ou null>}`. O usuário selecionado precisa estar ativo e ter membership no mesmo workspace; IDs externos ou inativos retornam 404. `GET /workspaces/{id}/members` lista membros ativos e seus papéis. Cada alteração efetiva registra autor, horário e mudança na auditoria. A atribuição não altera o estado de revisão nem concede permissão de aprovação. A lista de documentos aceita `?assigned_to_me=true` ou `?unassigned=true` (os filtros não podem ser combinados) e inclui responsável/autoria no resumo.

## Interface web

O frontend fica em `frontend/` e usa React, TypeScript e Vite. Requer Node.js 20.19 ou superior (ou 22.12 ou superior), conforme o requisito atual do Vite ([guia oficial](https://vite.dev/guide/)); o projeto foi instalado e validado com Node 24.15.0. Os testes da interface usam Vitest, que compartilha a configuração Vite ([guia oficial do Vitest](https://vitest.dev/guide/)).

Na fila, o filtro de responsável mostra todos os documentos, os atribuídos ao usuário autenticado ou os sem responsável. Operadores podem definir, trocar e remover responsáveis no detalhe; aprovadores sem papel `operator` veem o responsável, mas não recebem os controles de alteração.

O **Painel operacional** consulta apenas o workspace selecionado e abre com os últimos 14 dias; permite escolher outro período de até 366 dias. Os cartões mostram a fila atual de processamento/revisão/aprovação e os envios ao ERP no período; gráficos mostram o volume diário de documentos e os cinco códigos de divergência mais frequentes. Os estados vazios são exibidos sem substituir contagens por estimativas.

Configure a URL da API no ambiente Vite. O exemplo não contém segredo:

```bash
if [ ! -f frontend/.env.local ]; then cp frontend/.env.example frontend/.env.local; fi
```

Para desenvolvimento, siga [o guia de ambiente local](desenvolvimento-local.md), que cobre PostgreSQL, Redis, migrações, seed, workers e interface.

A interface abre em `http://localhost:5173`. A API permite CORS somente para `DOCUFLOW_FRONTEND_ORIGIN`, que por padrão é `http://localhost:5173`; configure a origem exata se alterar host ou porta. Origens vazias ou `*` são recusadas. Os cabeçalhos permitidos são `Authorization`, `Content-Type`, `X-Workspace-ID` e `Accept`; não há credenciais em cookies. O token Bearer fica apenas na memória da sessão React e é removido ao sair ou fechar/recarregar a página.

Login e papéis vêm de `GET /auth/me`; seleção de workspace sempre envia `X-Workspace-ID`. A interface usa os contratos atuais de lista/detalhe, revisão, auditoria, download e envio ERP. A lista exibe fornecedor e referência do pedido no resumo para permitir filtros antes que a extração termine; esses campos são consultados pelo backend no repositório do mesmo workspace. A revisão envia o documento completo com `expected_version`; conflito HTTP 409 pede recarga antes de uma nova ação. Aprovação e envio ao ERP são ações humanas explícitas.

O fluxo de processamento tem os estados `queued → processing → ready_for_review` ou `failed`. Redis/RQ recebe apenas o ID do documento como argumento; o worker carrega o pedido no workspace registrado e o arquivo pela chave interna. Se Redis estiver indisponível depois que o upload foi persistido, a API retorna HTTP 503, mantém o arquivo e o documento como `failed` com mensagem segura e permite nova tentativa. `POST /documents/{id}/reprocess` exige `operator`, associação ao workspace e estado `failed`; documentos prontos ou aprovados não são reprocessados. O worker confere o estado, workspace, ID RQ ativo e aprovação dentro do PostgreSQL antes de iniciar e antes de gravar o resultado. Isso impede que jobs repetidos ou antigos substituam resultados prontos, revisões ou snapshots aprovados; não oferece execução exatamente uma vez.

RQ aplica timeout por job e número máximo de tentativas configurável. Erros terminais deixam `processing_status: failed` e uma mensagem segura; o PDF e o histórico são mantidos. Se o worker cair de forma abrupta e o documento ficar em `processing`, o comando de recuperação busca execuções mais antigas que `DOCUFLOW_STALE_AFTER_SECONDS`, invalida o job antigo no banco e cria outro job. Se Redis ou o worker estiver indisponível, uploads podem permanecer em `queued` até que o serviço volte; falha de enqueue é registrada como `failed`, pronta para chamada explícita de reprocessamento.

`GET /documents` lista documentos do workspace selecionado e aceita `limit` (1–100) e `offset`. `GET /documents/{id}` retorna metadados e resultado persistido. `GET /documents/{id}/download` baixa o arquivo original. Os três endpoints exigem token e `X-Workspace-ID`; qualquer associação ao workspace permite consultar, mas os IDs são sempre filtrados pelo workspace e o download não expõe a chave interna.

## Revisão humana

Depois de uma análise concluída, `review_status` começa como `pending` e `review_version` como `1`. `processing_status` e `final_status` continuam representando o processamento e a validação comercial; revisão é um estado separado. A saída original permanece em `extracted_document`. Correções ficam em `reviewed_document` e em uma nova linha de histórico, com versão, usuário, data, justificativa e validação recalculada.

`PATCH /documents/{id}/review` exige o papel `operator` e recebe a versão lida, uma justificativa e o `ReferenceDocument` revisado completo. A rota permite corrigir documentos processados com sucesso, inclusive depois de uma rejeição ou aprovação. Cada correção incrementa `review_version`; se a versão enviada estiver desatualizada, a API retorna HTTP 409. A validação comercial existente é reaplicada, inclusive as evidências contra o texto das páginas. Uma nova correção depois de uma aprovação deixa `review_status` como `pending`; a cópia aprovada antiga continua preservada no histórico.

`POST /documents/{id}/approve` exige `approver` e `expected_version`. Antes de gravar, o backend busca novamente o pedido associado ao documento e revalida os dados revisados. Erros bloqueantes retornam HTTP 422. Uma aprovação grava um snapshot imutável dos dados e da validação, junto com a versão, o usuário e o horário. Uma divergência comercial pode ser analisada pelo aprovador; erros bloqueantes não podem ser aprovados.

`POST /documents/{id}/reject` exige `approver`, `expected_version` e justificativa não vazia. Aprovações e rejeições só podem ser decididas enquanto o estado está `pending`. Tanto correção quanto aprovação e rejeição exigem vínculo do usuário ao workspace selecionado. Quem tem apenas `approver` não pode corrigir; para executar as duas ações, a associação deve conter ambos os papéis.

`GET /documents/{id}/audit` retorna os eventos de revisão, correção, aprovação, rejeição e envio ERP (solicitação, tentativa, sucesso e falha) em ordem cronológica, com versão, autor, horário e justificativa quando aplicável. Os registros de correção, aprovação e auditoria são acrescentados ao histórico; o backend não edita os snapshots antigos. Padrões explícitos de senha/token/chave em formato `campo=valor` e tokens JWT em justificativas são mascarados antes de persistir.

## ERP de demonstração

O envio ao ERP é uma ação separada da aprovação. `POST /documents/{id}/erp-submission` exige token, `X-Workspace-ID`, papel `approver` e JSON `{"expected_version": 1}`. O backend recusa documentos pendentes/rejeitados, sem snapshot ou cuja versão atual não corresponda à versão aprovada. A solicitação é persistida antes de entrar na fila `erp-submissions`; o job RQ contém somente o ID da solicitação. `GET /erp-submissions/{id}` consulta o estado e exige o mesmo papel e workspace. IDs fora do workspace retornam 404.

O worker lê o snapshot imutável em `document_approvals`, nunca a extração nem uma revisão posterior. A tabela `erp_submissions` guarda estado (`queued`, `processing`, `succeeded` ou `failed`), versão aprovada, tentativas, identificador fictício e erro seguro. Solicitação, tentativa, sucesso e falha são eventos append-only visíveis em `GET /documents/{id}/audit`. Falhas podem ser retentadas repetindo o `POST` com a versão aprovada atual; a mesma solicitação e chave idempotente são reutilizadas. Jobs repetidos não reclamam uma solicitação já processada. Se o conteúdo mudar após o enqueue e antes do worker, o envio falha e exige nova aprovação.

`DemoERPClient` não conecta a sistemas externos nem faz lançamentos financeiros: ele simula o cadastro e produz um identificador estável `ERP-DEMO-...` pela chave de idempotência (workspace, documento e versão aprovada). A fila usa `ERP_REDIS_URL` (padrão: `REDIS_URL`), `DOCUFLOW_ERP_QUEUE_NAME` (`erp-submissions`) e `DOCUFLOW_ERP_JOB_TIMEOUT_SECONDS` (`60`). Inicie um worker separado da fila de documentos:

```bash
set -a
. ./.env
set +a
./.venv/bin/rq worker --url "${ERP_REDIS_URL:-$REDIS_URL}" "${DOCUFLOW_ERP_QUEUE_NAME:-erp-submissions}"
```

Depois que o worker termina, `GET /documents/{id}` retorna o documento extraído, avisos, validação e o estado comercial `blocked`, `divergent` ou `ready_for_review`. Divergências ficam persistidas para a revisão humana. Tipo e tamanho inválidos retornam HTTP 4xx antes do registro; pedido ausente retorna 404; falta de acesso retorna 403; indisponibilidade da fila retorna 503 com o ID do documento preservado. Falhas de PDF, timeout ou provedor são registradas como `failed` e podem ser consultadas sem expor detalhes internos.

Os papéis `operator` e `approver` são associações independentes. Um usuário pode ter ambos; `operator` analisa e corrige documentos, enquanto `approver` aprova ou rejeita. JWT usa HS256, expira em 30 minutos por padrão e exige uma chave definida no ambiente com ao menos 32 bytes. `JWT_SECRET_KEY` não tem valor padrão no código e a aplicação se recusa a iniciar sem uma chave válida. Para desenvolvimento local, gere uma chave com `openssl rand -hex 32`, coloque o resultado apenas em `.env` e ajuste `ACCESS_TOKEN_EXPIRE_MINUTES` se necessário. Não compartilhe nem versione `.env`.

O limite de upload é 10 MiB por padrão e o limite de PDF é cinco páginas. `UploadFile` mantém uploads grandes em arquivo temporário em vez de carregá-los integralmente na memória; a rota verifica o tamanho informado e lê no máximo limite + 1 bytes antes de processar. PNG e JPEG são tratados como uma única página. O extrator mantém texto PDF nativo a partir do mínimo configurado e chama OCR somente nas páginas abaixo desse limiar. A origem de cada página (`native` ou `ocr`) é persistida. PDF com senha, arquivo inválido e imagem de tipo/resolução incorretos falham com mensagens seguras. A validação de evidências compara a citação com o texto da página, mas texto OCR não confirma a precisão dos valores: a revisão humana continua necessária.

O worker lê as variáveis `DOCUFLOW_OCR_LANGUAGE` (`por+eng`), `DOCUFLOW_OCR_DPI` (`300`), `DOCUFLOW_OCR_TIMEOUT_SECONDS` (`30`), `DOCUFLOW_NATIVE_TEXT_MIN_CHARS` (`1`, contando caracteres após normalizar espaços) e `DOCUFLOW_TESSERACT_CMD` (`tesseract`). A CLI e os dados de idioma precisam estar instalados no host/container que executa o worker. Para renderização, o DPI deve ser pelo menos 72 e imagens maiores que 40 megapixels são recusadas para limitar memória.

A rota de upload consulta `PurchaseOrderRepository`; em execução normal, `SqlAlchemyPurchaseOrderRepository` usa as tabelas PostgreSQL. Após salvar o registro e o arquivo privado, a API enfileira apenas o ID do documento e responde HTTP 202. O worker carrega o pedido, o documento e o arquivo do PostgreSQL/armazenamento e então chama `DocumentProcessingService` com o `ExtractionProvider`. Esses componentes continuam substituíveis nos testes. O worker padrão usa `OllamaExtractionProvider`; configure endereço, modelo e timeout por `DOCUFLOW_OLLAMA_BASE_URL`, `DOCUFLOW_OLLAMA_MODEL` (padrão `gemma4`) e `DOCUFLOW_OLLAMA_TIMEOUT_SECONDS`. Os testes substituem o provedor por um stub e não chamam Ollama.

O armazenamento padrão fica em `var/documents`, fora de qualquer diretório público, e pode ser alterado por `DOCUMENT_STORAGE_DIR`. O `.gitignore` exclui esse diretório. Os nomes de arquivo são UUIDs internos por workspace; o nome original fica apenas como metadado. O PostgreSQL guarda metadados, SHA-256, status, contador de tentativas, ID ativo do job, texto de cada página, extração, avisos e validação em colunas relacionais/JSON. O PDF permanece no armazenamento privado e não é salvo como blob no banco.

## Extração estruturada

O serviço `StructuredExtractionService` recebe um provedor pela interface `ExtractionProvider`; o adaptador `OllamaExtractionProvider` usa `POST /api/chat` localmente e envia o schema de `ReferenceDocument` no campo `format`. Configure o nome do modelo e o timeout em `OllamaConfig`; `StructuredExtractionConfig.max_attempts` limita as tentativas de comunicação (padrão: 2). Os testes simulam o provedor e não exigem Ollama em execução.

`DocumentProcessingService` compõe a extração nativa/OCR por página, o serviço estruturado e a validação comercial. A extração estruturada recebe o número e a origem da página; o backend valida evidências na página correspondente antes da validação comercial. O serviço devolve o documento, avisos, relatório de validação e estado final; erros das etapas são propagados sem incluir os bytes do arquivo no resultado.

Os testes comuns substituem o OCR por mocks determinísticos e não precisam do Tesseract nem do Ollama. Para executar também o teste local opcional com Tesseract, confirme que o comando e os dados de idioma estão disponíveis, então rode:

```bash
DOCUFLOW_RUN_TESSERACT_TESTS=1 .venv/bin/python -m unittest discover -s backend/tests/integration -p 'test_ocr_tesseract_optional.py' -v
```

Sem `DOCUFLOW_RUN_TESSERACT_TESTS=1` ou sem o executável configurado, esse teste informa `skipped`.

## Importar pedidos por CSV

Usuários com papel `operator` podem abrir **Importar pedidos** no frontend. A prévia não grava dados; a confirmação envia o mesmo arquivo novamente, revalida fornecedores e referências no workspace e grava cada lote de pedidos e itens em uma transação. Fornecedores precisam estar cadastrados antes. Uma referência já existente com conteúdo diferente gera conflito, e uma cópia idêntica é ignorada. Cada confirmação gera um registro de auditoria com autor e contagens, sem guardar o conteúdo do CSV.

O arquivo deve ser UTF-8 (com ou sem BOM), usar vírgula ou ponto e vírgula e ter uma linha por item. Decimais aceitam ponto ou vírgula; com separador vírgula, coloque números com vírgula decimal entre aspas. Quantidades aceitam até três casas decimais; valores monetários, até duas. O limite do arquivo é 5 MB. O cabeçalho deve conter exatamente estas colunas:

```csv
purchase_order_reference,supplier_identifier,currency,order_total,item_code,item_description,quantity,unit_price,line_total
PC-2026-0100,FORN-0001,BRL,35.50,ARR-010,Arruela inox M8,2,10.25,20.50
PC-2026-0100,FORN-0001,BRL,35.50,PAR-010,Parafuso inox M8,1,15.00,15.00
```

Na API, `POST /purchase-orders/import/preview` recebe o arquivo multipart e mostra os pedidos, linhas válidas e erros com número de linha. `POST /purchase-orders/import/confirm` revalida e confirma a importação. Ambas exigem token Bearer, `X-Workspace-ID` e papel `operator`.

As verificações estão cobertas pelos testes unitários do backend e frontend. O teste PostgreSQL dedicado também valida persistência decimal, duplicata idempotente, auditoria e isolamento por workspace; execute-o com `TEST_DATABASE_URL` apontando para o banco local dedicado `docuflow_test`:

```bash
.venv/bin/python -m unittest backend.tests.test_purchase_order_csv_import -v
.venv/bin/python -m unittest discover -s backend/tests/integration -p test_postgres_integration.py -v
cd frontend && npm run test -- --run
```

## Regras comerciais por workspace

Membros com papel `operator` podem consultar e alterar a política do workspace em **Regras comerciais**. `GET /commercial-policy` retorna a política atual, `GET /commercial-policy/history` lista as versões e `PUT /commercial-policy` cria uma versão nova. A gravação exige `expected_version`; se outra alteração já tiver sido salva, a API retorna HTTP 409 e não sobrescreve a versão concorrente. Usuários sem papel `operator` não podem consultar nem alterar essa configuração.

A política controla somente as tolerâncias das três comparações de totais: quantidade × preço contra total do item, soma dos itens contra total do documento e total do documento contra total do pedido. O preço unitário comparado ao pedido continua com a tolerância fixa atual de R$ 0,01. Evidências, regras de aprovação e permissões não são configuráveis.

Workspaces sem configuração permanecem na versão 0, com tolerância padrão de `0.01`. Uma alteração salva uma versão imutável com mapa de moedas, autor e horário; esse histórico também é o registro de auditoria das mudanças da política. O mapa substitui as exceções anteriores. Moedas sem exceção explícita usam o padrão de `0.01`. Aceitamos códigos de três letras maiúsculas, até 20 moedas, e tolerâncias decimais entre `0` e `100.00`, com no máximo duas casas. Os valores são enviados como strings decimais; números JSON são recusados.

O worker captura a política atual ao iniciar cada processamento e grava sua cópia, incluindo o número da versão e as tolerâncias, dentro do resultado da validação. O detalhe do documento mostra essa versão. Correções e aprovação revalidam usando a cópia já aplicada ao documento, mesmo que a configuração do workspace tenha mudado depois. Alterar a política não reescreve validações existentes nem snapshots de aprovação; um novo processamento usa a versão vigente no momento em que o worker o inicia.

A migração `20261020_0010_workspace_commercial_policies` adiciona apenas a tabela append-only de versões e preserva os dados existentes. Para aplicá-la, use a configuração habitual do projeto e confira o destino de `DATABASE_URL` antes de executar:

```bash
.venv/bin/alembic -c backend/alembic.ini upgrade head
```

Os testes SQLite cobrem a política padrão, limites, moedas, histórico, isolamento, conflito por versão e preservação de snapshots. O teste de integração PostgreSQL também cobre persistência de autor/horário/mapa e duas alterações simultâneas com a mesma versão esperada. Execute-o somente com `TEST_DATABASE_URL` apontando para a base dedicada `docuflow_test`:

```bash
.venv/bin/python -m unittest backend.tests.test_commercial_policy -v
.venv/bin/python -m unittest discover -s backend/tests/integration -p test_postgres_integration.py -v
cd frontend && npm run test -- --run && npm run typecheck && npm run lint && npm run build
```
