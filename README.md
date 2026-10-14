# DocuFlow

Aplicação demonstrativa para conferir documentos comerciais fictícios de fornecedores contra pedidos de compra. A IA extrai campos com evidências; o backend valida as regras; uma pessoa revisa antes de qualquer lançamento.

A aplicação é demonstrativa, não é fiscal ou contábil certificada e exige revisão humana: texto reconhecido por OCR pode conter erros.

## Primeiro fluxo

O caso inicial, o pedido de compra, o contrato JSON de extração e os critérios de aceite estão em [`docs/01-fluxo-minimo.md`](docs/01-fluxo-minimo.md).

## Estrutura inicial

```text
backend/
  app/
    api/            # rotas HTTP
    integrations/   # adaptadores externos, incluindo IA
    models/         # entidades persistidas
    schemas/        # contratos Pydantic
    services/       # regras e casos de uso
  tests/
    integration/
    unit/
docs/               # decisões e guias do projeto
samples/documents/  # documentos fictícios para demonstração
```

## Etapas planejadas

1. Definir e aceitar o caso de referência.
2. API mínima, extração de PDF com texto, schema e validações.
3. Revisão humana e aprovação.
4. Persistência e processamento assíncrono com Redis + RQ.
5. OCR e ERP demonstrativo.
6. Avaliação, documentação e demonstração.

As dependências do backend são declaradas em `requirements.txt` e verificadas para Python 3.14.

## Dependências do backend

`pypdf` lê texto nativo de PDFs. Para páginas sem texto utilizável, `pypdfium2` renderiza a página localmente e o executável Tesseract faz OCR; Pillow valida e normaliza PNG/JPEG. O OCR não usa serviços externos e sua saída ainda passa pelas mesmas verificações de evidência e validação comercial. `pypdfium2` 5.14.0 e Pillow 12.3.0 foram instalados no Python 3.14.4 do projeto; os metadados oficiais declaram suporte a Python 3.14 ([pypdfium2 no PyPI](https://pypi.org/project/pypdfium2/), [Pillow no PyPI](https://pypi.org/project/pillow/)). A imagem do worker documental instala Tesseract e os dados `por` e `eng`; para executar o worker no host, instale o mecanismo e os idiomas ([instalação oficial](https://tesseract-ocr.github.io/tessdoc/Installation.html), [dados de idioma](https://tesseract-ocr.github.io/tessdoc/Data-Files.html)). No macOS use `brew install tesseract tesseract-lang`; em Debian/Ubuntu, `sudo apt install tesseract-ocr tesseract-ocr-por tesseract-ocr-eng`. A integração usa a CLI documentada pelo Tesseract ([uso da linha de comando](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)). As demais bibliotecas servem à API, autenticação, PostgreSQL e fila; RQ 2.12.0 e redis-py 8.1.0 também foram instalados e verificados em Python 3.14 ([documentação RQ](https://python-rq.org/docs/), [documentação redis-py](https://redis.readthedocs.io/en/stable/)). Para instalar as dependências Python e rodar os testes:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s backend/tests -v
```

## API HTTP

Inicie a aplicação localmente com:

```bash
.venv/bin/uvicorn backend.app.main:app --reload
```

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
cp frontend/.env.example frontend/.env.local
```

Para desenvolvimento, mantenha PostgreSQL, Redis, migrações, seed e workers conforme as seções abaixo. Inicie a API em um terminal e a interface em outro:

```bash
.venv/bin/uvicorn backend.app.main:app --reload
```

```bash
cd frontend
npm install
npm run dev
```

A interface abre em `http://localhost:5173`. A API permite CORS somente para `DOCUFLOW_FRONTEND_ORIGIN`, que por padrão é `http://localhost:5173`; configure a origem exata se alterar host ou porta. Origens vazias ou `*` são recusadas. Os cabeçalhos permitidos são `Authorization`, `Content-Type`, `X-Workspace-ID` e `Accept`; não há credenciais em cookies. O token Bearer fica apenas na memória da sessão React e é removido ao sair ou fechar/recarregar a página.

Login e papéis vêm de `GET /auth/me`; seleção de workspace sempre envia `X-Workspace-ID`. A interface usa os contratos atuais de lista/detalhe, revisão, auditoria, download e envio ERP. A lista exibe fornecedor e referência do pedido no resumo para permitir filtros antes que a extração termine; esses campos são consultados pelo backend no repositório do mesmo workspace. A revisão envia o documento completo com `expected_version`; conflito HTTP 409 pede recarga antes de uma nova ação. Aprovação e envio ao ERP são ações humanas explícitas.

Execute os testes, a verificação de tipos e o build da interface:

```bash
cd frontend
npm test
npm run typecheck
npm run build
```

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

## PostgreSQL e migrações

O desenvolvimento local usa `postgres:18.6-bookworm` e `redis:8.2.10-alpine3.22`, com versões fixadas, healthchecks e volumes nomeados `docuflow_local_postgres_data` e `docuflow_local_redis_data`. O Compose cria `docuflow` para a aplicação e executa `docker/postgres/init-test-db.sql` para criar `docuflow_test` na inicialização inicial do volume.

Copie o exemplo local e inicie PostgreSQL e Redis:

```bash
cp .env.example .env
docker compose up -d postgres redis
docker compose ps
```

O `.env` é ignorado pelo Git. `.env.example` contém valores apenas para desenvolvimento local; ajuste `POSTGRES_PORT` e as portas nas duas URLs se 5432 estiver ocupada. As variáveis precisam estar no ambiente do terminal para os comandos Python:

```bash
set -a
. ./.env
set +a
```

## Execução da stack com Docker Compose

O Compose também constrói a API, o worker documental, o worker da fila `erp-submissions` e o frontend. PostgreSQL e Redis continuam nos serviços e volumes nomeados existentes; o volume novo `docuflow_local_private_documents` é compartilhado somente pela API e pelo worker documental. O frontend recebe apenas os arquivos estáticos da build e não monta nem serve PDFs.

Prepare o `.env` antes de iniciar. Gere `JWT_SECRET_KEY` com `openssl rand -hex 32` e uma senha URL-safe para um banco novo com `openssl rand -hex 24`; use a senha em `POSTGRES_PASSWORD` e na URL `DOCUFLOW_CONTAINER_DATABASE_URL`. Se a senha tiver caracteres especiais, aplique percent-encoding na URL. Em um volume PostgreSQL existente, mantenha a senha que já inicializou o banco: mudar apenas a variável não altera a senha gravada no PostgreSQL. `.env.example` mostra os endereços internos `postgres` e `redis` usados pelos containers; `DATABASE_URL` e `REDIS_URL` sem o prefixo `DOCUFLOW_CONTAINER_` continuam apontando para o host quando comandos locais forem usados. Segredos reais ficam somente no `.env`, que está ignorado pelo Git.

Valide, construa e inicie a stack sem remover nem recriar os volumes atuais:

```bash
docker compose config --quiet
docker compose build api document-worker erp-worker frontend
docker compose up -d --wait postgres redis
docker compose run --rm --no-deps api alembic -c backend/alembic.ini upgrade head
docker compose run --rm --no-deps api python -m backend.app.seed_reference_order
docker compose up -d --wait api document-worker erp-worker frontend
docker compose ps
```

As migrações são sempre executadas explicitamente e nunca durante a inicialização da API. Se o banco já tiver estado desconhecido, confira-o antes do comando de migração. O primeiro usuário também é criado explicitamente. Esse comando pede a senha em prompt oculto e não recebe senha por argumento:

```bash
docker compose run --rm api python -m backend.app.admin create-user \
  --email operador@example.test --workspace demo \
  --role operator --role approver
```

Abra o frontend em `http://localhost:5173`; a API responde em `http://localhost:8000`. As duas portas são publicadas somente em loopback e a API permite apenas a origem configurada em `DOCUFLOW_FRONTEND_ORIGIN` (padrão `http://localhost:5173`). O Dockerfile Python usa Python 3.14.4, instala as versões exatas de `requirements.lock` e roda como UID não-root; o frontend usa `package-lock.json` e Nginx não privilegiado. Os containers de aplicação usam filesystem somente leitura, `no-new-privileges` e removem capabilities. O PostgreSQL e o Redis permanecem em seus volumes atuais; para parar sem apagar dados, use `docker compose stop` (nunca use `down -v`).

O worker documental recebe `DOCUFLOW_OCR_LANGUAGE=por+eng`, DPI, timeout e comando já configuráveis. Confira os idiomas instalados no container com `docker compose exec document-worker tesseract --list-langs`. O adaptador ERP continua sendo `DemoERPClient`; nenhum serviço externo é contatado.

O build não instala nem baixa Ollama ou modelos. Para usar Ollama, configure `DOCUFLOW_OLLAMA_BASE_URL` (no exemplo `http://host.docker.internal:11434`), `DOCUFLOW_OLLAMA_MODEL` e `DOCUFLOW_OLLAMA_TIMEOUT_SECONDS` no `.env`, inicie Ollama no host e baixe o modelo manualmente com `ollama pull <modelo>`. O worker documental precisa conseguir alcançar a URL configurada; firewall, processo Ollama e modelo são configuração do host. Sem Ollama, os testes usam provedor determinístico.

O teste Playwright pode manter API e os dois workers em containers enquanto usa uma fila Redis e a base PostgreSQL dedicadas. Ele não inicia nem encerra PostgreSQL/Redis, e usa uma rede externa já criada pelo Compose. Para habilitá-lo, instale Chromium como na seção abaixo, carregue `.env`, garanta que `docuflow_test` e Redis `/15` estão disponíveis e execute:

```bash
set -a
. ./.env
set +a
export DOCUFLOW_RUN_BROWSER_E2E=1
export DOCUFLOW_BROWSER_E2E_DOCKER=1
cd frontend
npm run test:e2e
```

Esse modo cria um projeto Compose e portas exclusivos, usa um diretório temporário privado, provedor estruturado determinístico e ERP simulado, e remove apenas os containers, arquivos, registros identificados e chaves de fila criados pelo próprio cenário. Ele é ignorado se Docker, rede Compose, PostgreSQL/Redis de teste ou Chromium não estiverem disponíveis. O caminho padrão sem `DOCUFLOW_BROWSER_E2E_DOCKER=1` continua executando os processos locais.

As URLs usam Psycopg 3 e apontam para bases diferentes. Aplique as migrações e cadastre o pedido fictício de forma explícita:

```bash
.venv/bin/alembic -c backend/alembic.ini upgrade head
.venv/bin/python -m backend.app.seed_reference_order
.venv/bin/uvicorn backend.app.main:app --reload
```

Em outro terminal, inicie o worker RQ para consumir a fila `documents`:

```bash
set -a
. ./.env
set +a
./.venv/bin/rq worker --url "$REDIS_URL" "$DOCUFLOW_QUEUE_NAME"
```

Instale as dependências OCR no mesmo host onde esse worker será executado. O padrão é `DOCUFLOW_OCR_LANGUAGE=por+eng`; o executável é configurável por `DOCUFLOW_TESSERACT_CMD`, o DPI por `DOCUFLOW_OCR_DPI` e o limite de cada chamada por `DOCUFLOW_OCR_TIMEOUT_SECONDS`. O `.env.example` contém esses valores ilustrativos. Para conferir se o comando e os dados de idioma estão disponíveis antes de iniciar o worker:

```bash
tesseract --version
tesseract --list-langs
```

Na lista, confirme que aparecem `por` e `eng`. Se a execução informar `Failed loading language 'por'` ou `Error opening data file`, instale o pacote de dados do idioma no host do worker (`brew install tesseract-lang` no macOS ou `sudo apt install tesseract-ocr-por` no Debian/Ubuntu) e confira novamente. Se `tesseract` não for encontrado no `PATH`, configure `DOCUFLOW_TESSERACT_CMD` com o caminho absoluto do executável. Quando `TESSDATA_PREFIX` for necessário em uma instalação personalizada, configure-o no ambiente do worker para apontar ao diretório pai de `tessdata`.

Para recuperar documentos que ficaram presos depois de uma interrupção do worker:

```bash
set -a
. ./.env
set +a
./.venv/bin/python -m backend.app.recover_documents
```

O timeout padrão é 300 segundos, o máximo é 3 tentativas e o limiar de recuperação é 900 segundos. Ajuste `DOCUFLOW_JOB_TIMEOUT_SECONDS`, `DOCUFLOW_MAX_ATTEMPTS` e `DOCUFLOW_STALE_AFTER_SECONDS` no `.env`; `REDIS_URL`, `REDIS_PORT` e `DOCUFLOW_QUEUE_NAME` também são configuráveis. O limiar de recuperação deve ser maior que o timeout dos jobs.

O seed pode ser executado mais de uma vez: se o pedido `PC-2026-0001` já existir, ele informa isso e não o duplica. A migração `20261013_0002` cria o workspace `demo` e associa a ele todos os fornecedores e pedidos já existentes, preservando seus IDs, itens e demais dados. A migração `20261014_0003` adiciona documentos sem apagar dados e restringe cada documento ao workspace e ao pedido correspondente por chave estrangeira composta. A migração `20261015_0004` amplia o estado da revisão e cria tabelas separadas para snapshots revisados, aprovações e auditoria; os documentos existentes recebem versão inicial `1`. A migração `20261016_0005` adiciona fila, tentativas e ID do job ativo; converte os registros `succeeded` para `ready_for_review` e preserva resultados e snapshots. A migração `20261016_0006` muda o default do banco para `queued`. A migração `20261017_0007` adiciona solicitações ERP com unicidade por workspace/documento/versão aprovada e amplia os tipos de evento de auditoria, sem alterar dados ou snapshots existentes. A aplicação não cria tabelas nem dados durante a inicialização. Os modelos ficam em `backend/app/models/`; as revisões ficam em `backend/migrations/versions/`. Valores de quantidade e dinheiro usam `NUMERIC` no banco e `Decimal` em Python. As credenciais não devem ser adicionadas ao arquivo Alembic nem ao código.

Depois da migração, o workspace `demo` já existe. Crie o primeiro usuário pelo comando administrativo; ele pede a senha de forma interativa duas vezes, sem colocá-la em argumentos ou arquivos versionados:

```bash
.venv/bin/python -m backend.app.admin create-user \
  --email operador@example.test \
  --workspace demo \
  --role operator \
  --role approver
```

Para criar outro workspace antes de associar usuários a ele:

```bash
.venv/bin/python -m backend.app.admin create-workspace --slug filial --name "Filial"
.venv/bin/python -m backend.app.admin create-user --email filial@example.test --workspace filial --role operator
.venv/bin/python -m backend.app.admin add-membership --email operador@example.test --workspace filial --role approver
```

### Testes

Os testes unitários usam SQLite temporário e adaptadores de fila/provedor simulados; não precisam de Redis, Ollama ou `DATABASE_URL`:

```bash
./.venv/bin/python -m unittest discover -s backend/tests -v
```

Para executar somente os testes do fluxo ERP:

```bash
./.venv/bin/python -m unittest backend.tests.test_erp_submission -v
```

Os testes de integração PostgreSQL rodam separadamente contra a base local dedicada `docuflow_test`. Eles aplicam apenas `upgrade head`, não removem tabelas nem limpam dados; os registros criados permanecem nessa base. O helper recusa SQLite, outros nomes de banco e uma URL igual a `DATABASE_URL`.

```bash
./.venv/bin/python -m unittest discover -s backend/tests/integration -v
```

Esse grupo inclui a migração de atribuição e um teste PostgreSQL que cria um documento isolado, troca/remove o responsável e confere filtros e eventos de auditoria. Configure `TEST_DATABASE_URL` para `docuflow_test` antes de executar; sem a variável, valide a regra equivalente pelos testes unitários SQLite.

A integração ERP usa essa base dedicada e a Redis de teste, com uma fila de nome aleatório. Ela valida a migração, a persistência e um worker RQ real em `burst`; o adaptador permanece local e determinístico, sem ERP externo. Não usa a fila de documentos nem recuperação global.

O teste `test_rq_real_integration` também usa Redis real quando `TEST_REDIS_URL` está definida. Ele cria um nome de fila aleatório, executa um worker RQ em modo burst e usa um provedor determinístico exclusivo dos testes; não chama Ollama. Se `TEST_DATABASE_URL` ou `TEST_REDIS_URL` estiver ausente, o teste é marcado como ignorado. O padrão do `.env.example` separa a base lógica Redis de teste (`/15`) da fila normal (`/0`). A limpeza remove apenas jobs e chaves da fila aleatória criada pela execução; não usa `FLUSHDB` nem apaga volumes.

Para executar o teste real, inicie PostgreSQL e Redis do Compose sem recriar volumes, carregue as variáveis e rode a integração:

```bash
docker compose up -d postgres redis
set -a
. ./.env
set +a
./.venv/bin/python -m unittest discover -s backend/tests/integration -v
```

Os testes unitários continuam sem depender de Redis. A integração real verifica que documento e arquivo existem antes do enqueue, que o job contém apenas o ID, a transição pelo worker, job repetido e recuperação após interrupção simulada.

O teste completo de OCR pela fila é opt-in. Além da base e do Redis de teste, ele exige `DOCUFLOW_RUN_TESSERACT_TESTS=1`, o executável configurado e todos os idiomas de `DOCUFLOW_OCR_LANGUAGE`. Ele gera um PDF somente-imagem, usa uma fila exclusiva, roda o worker RQ real em `burst` e chama Tesseract real; o provedor estruturado é determinístico e exclusivo do teste. O teste remove somente o job, as chaves da sua fila e o arquivo temporário criado por ele.

```bash
docker compose up -d postgres redis
set -a
. ./.env
set +a
export TEST_REDIS_URL=redis://localhost:6379/15
export DOCUFLOW_RUN_TESSERACT_TESTS=1
export DOCUFLOW_OCR_LANGUAGE=por+eng
./.venv/bin/python -m unittest \
  backend.tests.integration.test_rq_real_integration.RealRQPostgreSQLIntegrationTests.test_real_rq_ocr_upload_uses_tesseract_and_validates_page_evidence \
  -v
```

Ele é ignorado se `TEST_DATABASE_URL`, `TEST_REDIS_URL` ou `DOCUFLOW_RUN_TESSERACT_TESTS=1` estiver ausente; também é ignorado se o executável ou algum idioma configurado não estiver disponível.

O SQL de inicialização do Compose cria a base de teste somente quando o volume PostgreSQL é inicializado pela primeira vez. Se estiver usando um volume local existente no qual essa base ainda não existe, crie-a sem apagar o volume com `docker compose exec postgres createdb -U docuflow docuflow_test` (ajuste o usuário se mudou `POSTGRES_USER`). Para parar o serviço mantendo todos os dados, use `docker compose stop postgres`; para iniciá-lo novamente, `docker compose start postgres`. Não use `down -v` para encerrar o ambiente.

## Extração estruturada

O serviço `StructuredExtractionService` recebe um provedor pela interface `ExtractionProvider`; o adaptador `OllamaExtractionProvider` usa `POST /api/chat` localmente e envia o schema de `ReferenceDocument` no campo `format`. Configure o nome do modelo e o timeout em `OllamaConfig`; `StructuredExtractionConfig.max_attempts` limita as tentativas de comunicação (padrão: 2). Os testes simulam o provedor e não exigem Ollama em execução.

`DocumentProcessingService` compõe a extração nativa/OCR por página, o serviço estruturado e a validação comercial. A extração estruturada recebe o número e a origem da página; o backend valida evidências na página correspondente antes da validação comercial. O serviço devolve o documento, avisos, relatório de validação e estado final; erros das etapas são propagados sem incluir os bytes do arquivo no resultado.

Os testes comuns substituem o OCR por mocks determinísticos e não precisam do Tesseract nem do Ollama. Para executar também o teste local opcional com Tesseract, confirme que o comando e os dados de idioma estão disponíveis, então rode:

```bash
DOCUFLOW_RUN_TESSERACT_TESTS=1 .venv/bin/python -m unittest discover -s backend/tests/integration -p 'test_ocr_tesseract_optional.py' -v
```

Sem `DOCUFLOW_RUN_TESSERACT_TESTS=1` ou sem o executável configurado, esse teste informa `skipped`.

### Teste ponta a ponta no navegador

O Playwright executa o fluxo principal em Chromium real: login, escolha do workspace, upload, processamento RQ, leitura de evidências e achados, revisão versionada, aprovação humana e envio ao ERP de demonstração. O cenário usa um provedor estruturado determinístico e `DemoERPClient`; não chama Ollama, ERP externo nem requer Tesseract, pois o PDF gerado tem texto nativo.

Instale as dependências do frontend e o navegador suportado pelo Playwright:

```bash
cd frontend
npm install
npx playwright install chromium
```

Configure `.env` com `DATABASE_URL` apontando para a base de desenvolvimento, `TEST_DATABASE_URL` apontando para PostgreSQL local `docuflow_test`, e `TEST_REDIS_URL` apontando para Redis local em uma base lógica diferente da fila normal (o exemplo usa `/15`, enquanto a fila normal usa `/0`). Inicie somente os serviços compartilhados necessários; o teste não os apaga nem os encerra:

```bash
cd "$(git rev-parse --show-toplevel)"
docker compose up -d postgres redis
```

Execute o cenário de forma explícita:

```bash
set -a
. ./.env
set +a
export DOCUFLOW_RUN_BROWSER_E2E=1
cd frontend
npm run test:e2e
```

O comando inicia API, Vite e um worker de teste como processos filhos, em portas locais escolhidas para a execução, e os encerra ao terminar. O worker escuta duas filas com nomes aleatórios exclusivos; o teste remove apenas jobs e chaves dessas filas. A migração roda com `upgrade head` em `docuflow_test`; usuário, workspaces, fornecedor, pedido, documento, revisão e aprovação usam identificadores únicos e são removidos ao final. Arquivos ficam em diretório temporário do cenário. A rotina recusa outro nome de banco, host remoto e Redis que compartilhe a mesma base lógica da fila normal.

O teste é ignorado com motivo explícito se `TEST_DATABASE_URL`, `TEST_REDIS_URL`, `DOCUFLOW_RUN_BROWSER_E2E=1`, `.venv` ou Chromium estiverem ausentes; também informa quando PostgreSQL ou Redis configurado está indisponível. A suíte Vitest continua restrita a `frontend/src` e não inicia navegador ou serviços. Playwright usa Node.js 22, 24 ou 26 segundo os requisitos oficiais ([instalação do Playwright](https://playwright.dev/docs/intro)); este projeto foi validado com Node 24.

## CI

O GitHub Actions está configurado em `.github/workflows/ci.yml` para pull requests e pushes em `main`. O estado de cada execução aparece na aba **Actions** do repositório. Os jobs são: testes unitários Python, verificações e build do frontend, integração com PostgreSQL/Redis e OCR real, fluxo Playwright em Chromium com containers, e validação/build do Compose. As integrações usam serviços, filas e volumes específicos da execução; os workflows não carregam `.env`.

Para reproduzir as verificações localmente, instale as dependências travadas e execute as mesmas suítes:

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m unittest discover -s backend/tests -v

cd frontend
npm ci
npm run test
npm run typecheck
npm run lint
npm run build
```

Os testes PostgreSQL/Redis e o Playwright precisam de serviços dedicados. O exemplo abaixo cria um projeto Compose e volumes próprios em portas alternativas; os valores de senha e JWT são fictícios para uso local. Não carregue o `.env` normal nesse ambiente:

```bash
cd ..
export COMPOSE_PROJECT_NAME="docuflow-ci-local-$(date +%s)"
export DOCUFLOW_CI_POSTGRES_VOLUME="${COMPOSE_PROJECT_NAME}-postgres"
export DOCUFLOW_CI_REDIS_VOLUME="${COMPOSE_PROJECT_NAME}-redis"
export DOCUFLOW_CI_DOCUMENTS_VOLUME="${COMPOSE_PROJECT_NAME}-documents"
export POSTGRES_DB=docuflow POSTGRES_USER=docuflow POSTGRES_PASSWORD=ci_local_only
export POSTGRES_PORT=15432 REDIS_PORT=16379
export DATABASE_URL=postgresql+psycopg://docuflow:ci_local_only@127.0.0.1:15432/docuflow
export TEST_DATABASE_URL=postgresql+psycopg://docuflow:ci_local_only@127.0.0.1:15432/docuflow_test
export REDIS_URL=redis://127.0.0.1:16379/0 ERP_REDIS_URL=redis://127.0.0.1:16379/0
export TEST_REDIS_URL=redis://127.0.0.1:16379/15
export DOCUFLOW_CONTAINER_DATABASE_URL=postgresql+psycopg://docuflow:ci_local_only@postgres:5432/docuflow
export DOCUFLOW_CONTAINER_REDIS_URL=redis://redis:6379/0
export JWT_SECRET_KEY=ci_local_only_not_a_real_secret
export DOCUFLOW_FRONTEND_ORIGIN=http://localhost:5173

docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" \
  -f compose.yaml -f .github/compose.ci.yaml up -d --wait postgres redis
.venv/bin/python -m unittest discover -s backend/tests/integration -v
```

Para executar também o OCR real, instale Tesseract com os idiomas `por` e `eng` no host e rode:

```bash
cd "$(git rev-parse --show-toplevel)"
export DOCUFLOW_OCR_LANGUAGE=por+eng
export DOCUFLOW_RUN_TESSERACT_TESTS=1
.venv/bin/python -m unittest \
  backend.tests.integration.test_rq_real_integration.RealRQPostgreSQLIntegrationTests.test_real_rq_ocr_upload_uses_tesseract_and_validates_page_evidence \
  -v
.venv/bin/python -m unittest discover -s backend/tests/integration -p test_ocr_tesseract_optional.py -v
```

Para o E2E em Chromium com API e workers containerizados, instale as dependências de frontend e o navegador, depois use a rede do projeto isolado:

```bash
cd "$(git rev-parse --show-toplevel)"
cd frontend
npm ci
npx playwright install chromium
export DOCUFLOW_E2E_DOCKER_NETWORK="${COMPOSE_PROJECT_NAME}_default"
export DOCUFLOW_RUN_BROWSER_E2E=1 DOCUFLOW_BROWSER_E2E_DOCKER=1
npm run test:e2e
```

Para validar o Compose e construir as imagens, execute:

```bash
cd "$(git rev-parse --show-toplevel)"
docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f .github/compose.ci.yaml config --quiet
docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f .github/compose.ci.yaml build api document-worker erp-worker frontend
```

Ao terminar a reprodução local, `docker compose --env-file /dev/null -p "$COMPOSE_PROJECT_NAME" -f compose.yaml -f .github/compose.ci.yaml stop postgres redis` para os serviços mantendo os volumes; não use comandos que removam volumes.

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
