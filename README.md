# DocuFlow

Aplicação demonstrativa para conferir documentos comerciais fictícios de fornecedores contra pedidos de compra. A IA extrai campos com evidências; o backend valida as regras; uma pessoa revisa antes de qualquer lançamento.

O sistema não é fiscal ou contábil certificado e não processa documentos reais nesta etapa.

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

`pypdf` lê o texto nativo dos PDFs localmente, sem OCR nem dependências de sistema. `FastAPI` expõe as rotas; `python-multipart` analisa formulários com arquivos; `httpx` atende ao `TestClient`; `uvicorn` inicia o servidor ASGI; SQLAlchemy e Psycopg conectam ao PostgreSQL; Alembic aplica migrações; `pwdlib[argon2]` armazena senhas com Argon2id; PyJWT assina e valida tokens; `rq` executa jobs e `redis` conecta ao Redis. RQ 2.12.0 e redis-py 8.1.0 foram instalados e verificados em Python 3.14 ([documentação RQ: jobs, timeout e tentativas](https://python-rq.org/docs/), [documentação redis-py](https://redis.readthedocs.io/en/stable/), [metadados Python do RQ](https://pypi.org/project/rq/2.12.0/), [metadados Python do redis-py](https://pypi.org/project/redis/)). As versões instaladas e verificadas no ambiente incluem FastAPI 0.143.0, python-multipart 0.0.32, httpx 0.28.1, Uvicorn 0.54.0, SQLAlchemy 2.1.4, Alembic 1.20.0, Psycopg 3.3.6, pwdlib 0.3.1, argon2-cffi 25.1.0 e PyJWT 2.15.1. Para instalar as dependências e rodar os testes:

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

`GET /health` retorna `{"status":"ok"}`. `POST /auth/token` recebe formulário OAuth2 com `username` (e-mail) e `password`, e retorna um token Bearer. `GET /auth/me` exige esse token e lista o usuário, workspaces e papéis disponíveis. `POST /documents/analyze` também exige `Authorization: Bearer <token>` e o cabeçalho `X-Workspace-ID`. O formulário multipart contém `file` (`application/pdf`) e `purchase_order_reference` (por exemplo, `PC-2026-0001`). O backend confirma o vínculo do usuário ao workspace, exige o papel `operator` e só então busca o pedido naquele workspace. O repositório aplica o filtro de workspace na consulta.

`POST /documents/analyze` valida tipo e tamanho, grava o registro e o PDF privado e só então enfileira o job. A resposta inicial é HTTP 202 com `document_id` e `state: queued`. `review_status` continua independente e começa como `pending`; nenhum documento é aprovado automaticamente. Reenviar bytes com o mesmo SHA-256 no mesmo workspace retorna HTTP 200 com `duplicate: true` e o ID já registrado. O mesmo arquivo pode ser registrado em outro workspace.

O fluxo de processamento tem os estados `queued → processing → ready_for_review` ou `failed`. Redis/RQ recebe apenas o ID do documento como argumento; o worker carrega o pedido no workspace registrado e o PDF pela chave interna. Se Redis estiver indisponível depois que o upload foi persistido, a API retorna HTTP 503, mantém o arquivo e o documento como `failed` com mensagem segura e permite nova tentativa. `POST /documents/{id}/reprocess` exige `operator`, associação ao workspace e estado `failed`; documentos prontos ou aprovados não são reprocessados. O worker confere o estado, workspace, ID RQ ativo e aprovação dentro do PostgreSQL antes de iniciar e antes de gravar o resultado. Isso impede que jobs repetidos ou antigos substituam resultados prontos, revisões ou snapshots aprovados; não oferece execução exatamente uma vez.

RQ aplica timeout por job e número máximo de tentativas configurável. Erros terminais deixam `processing_status: failed` e uma mensagem segura; o PDF e o histórico são mantidos. Se o worker cair de forma abrupta e o documento ficar em `processing`, o comando de recuperação busca execuções mais antigas que `DOCUFLOW_STALE_AFTER_SECONDS`, invalida o job antigo no banco e cria outro job. Se Redis ou o worker estiver indisponível, uploads podem permanecer em `queued` até que o serviço volte; falha de enqueue é registrada como `failed`, pronta para chamada explícita de reprocessamento.

`GET /documents` lista documentos do workspace selecionado e aceita `limit` (1–100) e `offset`. `GET /documents/{id}` retorna metadados e resultado persistido. `GET /documents/{id}/download` baixa o PDF original. Os três endpoints exigem token e `X-Workspace-ID`; qualquer associação ao workspace permite consultar, mas os IDs são sempre filtrados pelo workspace e o download não expõe a chave interna.

## Revisão humana

Depois de uma análise concluída, `review_status` começa como `pending` e `review_version` como `1`. `processing_status` e `final_status` continuam representando o processamento e a validação comercial; revisão é um estado separado. A saída original permanece em `extracted_document`. Correções ficam em `reviewed_document` e em uma nova linha de histórico, com versão, usuário, data, justificativa e validação recalculada.

`PATCH /documents/{id}/review` exige o papel `operator` e recebe a versão lida, uma justificativa e o `ReferenceDocument` revisado completo. A rota permite corrigir documentos processados com sucesso, inclusive depois de uma rejeição ou aprovação. Cada correção incrementa `review_version`; se a versão enviada estiver desatualizada, a API retorna HTTP 409. A validação comercial existente é reaplicada, inclusive as evidências contra o texto das páginas. Uma nova correção depois de uma aprovação deixa `review_status` como `pending`; a cópia aprovada antiga continua preservada no histórico.

`POST /documents/{id}/approve` exige `approver` e `expected_version`. Antes de gravar, o backend busca novamente o pedido associado ao documento e revalida os dados revisados. Erros bloqueantes retornam HTTP 422. Uma aprovação grava um snapshot imutável dos dados e da validação, junto com a versão, o usuário e o horário. Uma divergência comercial pode ser analisada pelo aprovador; erros bloqueantes não podem ser aprovados.

`POST /documents/{id}/reject` exige `approver`, `expected_version` e justificativa não vazia. Aprovações e rejeições só podem ser decididas enquanto o estado está `pending`. Tanto correção quanto aprovação e rejeição exigem vínculo do usuário ao workspace selecionado. Quem tem apenas `approver` não pode corrigir; para executar as duas ações, a associação deve conter ambos os papéis.

`GET /documents/{id}/audit` retorna os eventos de revisão, correção, aprovação e rejeição em ordem cronológica, com versão, autor, horário e justificativa quando aplicável. Os registros de correção, aprovação e auditoria são acrescentados ao histórico; o backend não edita os snapshots antigos. Padrões explícitos de senha/token/chave em formato `campo=valor` e tokens JWT em justificativas são mascarados antes de persistir.

Depois que o worker termina, `GET /documents/{id}` retorna o documento extraído, avisos, validação e o estado comercial `blocked`, `divergent` ou `ready_for_review`. Divergências ficam persistidas para a revisão humana. Tipo e tamanho inválidos retornam HTTP 4xx antes do registro; pedido ausente retorna 404; falta de acesso retorna 403; indisponibilidade da fila retorna 503 com o ID do documento preservado. Falhas de PDF, timeout ou provedor são registradas como `failed` e podem ser consultadas sem expor detalhes internos.

Os papéis `operator` e `approver` são associações independentes. Um usuário pode ter ambos; `operator` analisa e corrige documentos, enquanto `approver` aprova ou rejeita. JWT usa HS256, expira em 30 minutos por padrão e exige uma chave definida no ambiente com ao menos 32 bytes. `JWT_SECRET_KEY` não tem valor padrão no código e a aplicação se recusa a iniciar sem uma chave válida. Para desenvolvimento local, gere uma chave com `openssl rand -hex 32`, coloque o resultado apenas em `.env` e ajuste `ACCESS_TOKEN_EXPIRE_MINUTES` se necessário. Não compartilhe nem versione `.env`.

O limite de upload é 10 MiB por padrão. `UploadFile` mantém uploads grandes em arquivo temporário em vez de carregá-los integralmente na memória; a rota verifica o tamanho informado e lê no máximo limite + 1 bytes antes de processar. O serviço de PDF existente também aplica seu limite.

A rota de upload consulta `PurchaseOrderRepository`; em execução normal, `SqlAlchemyPurchaseOrderRepository` usa as tabelas PostgreSQL. Após salvar o registro e o arquivo privado, a API enfileira apenas o ID do documento e responde HTTP 202. O worker carrega o pedido, o documento e o arquivo do PostgreSQL/armazenamento e então chama `DocumentProcessingService` com o `ExtractionProvider`. Esses componentes continuam substituíveis nos testes. O worker padrão usa `OllamaExtractionProvider`; configure o modelo com `DOCUFLOW_OLLAMA_MODEL` (padrão `gemma4`). Os testes substituem o provedor por um stub e não chamam Ollama.

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

Para recuperar documentos que ficaram presos depois de uma interrupção do worker:

```bash
set -a
. ./.env
set +a
./.venv/bin/python -m backend.app.recover_documents
```

O timeout padrão é 300 segundos, o máximo é 3 tentativas e o limiar de recuperação é 900 segundos. Ajuste `DOCUFLOW_JOB_TIMEOUT_SECONDS`, `DOCUFLOW_MAX_ATTEMPTS` e `DOCUFLOW_STALE_AFTER_SECONDS` no `.env`; `REDIS_URL`, `REDIS_PORT` e `DOCUFLOW_QUEUE_NAME` também são configuráveis. O limiar de recuperação deve ser maior que o timeout dos jobs.

O seed pode ser executado mais de uma vez: se o pedido `PC-2026-0001` já existir, ele informa isso e não o duplica. A migração `20261013_0002` cria o workspace `demo` e associa a ele todos os fornecedores e pedidos já existentes, preservando seus IDs, itens e demais dados. A migração `20261014_0003` adiciona documentos sem apagar dados e restringe cada documento ao workspace e ao pedido correspondente por chave estrangeira composta. A migração `20261015_0004` amplia o estado da revisão e cria tabelas separadas para snapshots revisados, aprovações e auditoria; os documentos existentes recebem versão inicial `1`. A migração `20261016_0005` adiciona fila, tentativas e ID do job ativo; converte os registros `succeeded` para `ready_for_review` e preserva resultados e snapshots. A migração `20261016_0006` muda o default do banco para `queued`. A aplicação não cria tabelas nem dados durante a inicialização. Os modelos ficam em `backend/app/models/`; as revisões ficam em `backend/migrations/versions/`. Valores de quantidade e dinheiro usam `NUMERIC` no banco e `Decimal` em Python. As credenciais não devem ser adicionadas ao arquivo Alembic nem ao código.

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

Os testes de integração rodam separadamente contra o PostgreSQL local e exigem `TEST_DATABASE_URL` apontando para `localhost` e para o banco dedicado `docuflow_test`. Eles aplicam apenas `upgrade head`, não removem tabelas nem limpam dados; os registros criados pelos testes permanecem nessa base de teste. O helper também recusa SQLite, outros nomes de banco e uma URL igual a `DATABASE_URL`. Nenhum teste exige um Redis real: o contrato do adaptador RQ usa fila simulada, e as proteções de estado e recuperação usam repositórios SQLite/PostgreSQL.

```bash
./.venv/bin/python -m unittest discover -s backend/tests/integration -v
```

O SQL de inicialização do Compose cria a base de teste somente quando o volume PostgreSQL é inicializado pela primeira vez. Se estiver usando um volume local existente no qual essa base ainda não existe, crie-a sem apagar o volume com `docker compose exec postgres createdb -U docuflow docuflow_test` (ajuste o usuário se mudou `POSTGRES_USER`). Para parar o serviço mantendo todos os dados, use `docker compose stop postgres`; para iniciá-lo novamente, `docker compose start postgres`. Não use `down -v` para encerrar o ambiente.

## Extração estruturada

O serviço `StructuredExtractionService` recebe um provedor pela interface `ExtractionProvider`; o adaptador `OllamaExtractionProvider` usa `POST /api/chat` localmente e envia o schema de `ReferenceDocument` no campo `format`. Configure o nome do modelo e o timeout em `OllamaConfig`; `StructuredExtractionConfig.max_attempts` limita as tentativas de comunicação (padrão: 2). Os testes simulam o provedor e não exigem Ollama em execução.

`DocumentProcessingService` compõe o extrator de PDF, o serviço estruturado e a validação comercial. Ele devolve o documento, avisos do PDF, relatório de validação e estado final; erros das etapas são propagados sem incluir os bytes do arquivo no resultado.
