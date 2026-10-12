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
4. Persistência e processamento assíncrono.
5. OCR, ERP demonstrativo e recuperação de falhas.
6. Avaliação, documentação e demonstração.

As dependências do backend são declaradas em `requirements.txt` e verificadas para a versão de Python do ambiente.

## Dependências do backend

`pypdf` lê o texto nativo dos PDFs localmente, sem OCR nem dependências de sistema. `FastAPI` expõe as rotas; `python-multipart` analisa formulários com arquivos; `httpx` atende ao `TestClient`; `uvicorn` inicia o servidor ASGI; SQLAlchemy e Psycopg conectam ao PostgreSQL; Alembic aplica migrações; `pwdlib[argon2]` armazena senhas com Argon2id; e PyJWT assina e valida tokens. As versões instaladas e verificadas no ambiente Python 3.14 incluem FastAPI 0.143.0, python-multipart 0.0.32, httpx 0.28.1, Uvicorn 0.54.0, SQLAlchemy 2.1.4, Alembic 1.20.0, Psycopg 3.3.6, pwdlib 0.3.1, argon2-cffi 25.1.0 e PyJWT 2.15.1. Os projetos de autenticação declaram compatibilidade com Python 3.14 ([documentação FastAPI de OAuth2/JWT](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/), [pwdlib](https://pypi.org/project/pwdlib/), [argon2-cffi](https://github.com/hynek/argon2-cffi/blob/main/CHANGELOG.md), [PyJWT](https://pypi.org/project/PyJWT/)). Para instalar as dependências e rodar os testes:

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

A análise retorna o documento extraído, avisos do PDF, achados da validação comercial e o estado `blocked`, `divergent` ou `ready_for_review`. Uma divergência retorna HTTP 200 com os achados no corpo e nunca aprova nem lança o documento. Erros de tipo, tamanho, PDF inválido e pedido ausente retornam respostas HTTP 4xx; falta de acesso retorna 403; falha ou timeout do provedor retorna 502 ou 504. Erros inesperados retornam uma mensagem genérica sem detalhes internos.

Os papéis `operator` e `approver` são associações independentes. Um usuário pode ter os dois; nesta etapa, somente `operator` habilita análise e `approver` ainda não executa aprovação. JWT usa HS256, expira em 30 minutos por padrão e exige uma chave definida no ambiente com ao menos 32 bytes. `JWT_SECRET_KEY` não tem valor padrão no código e a aplicação se recusa a iniciar sem uma chave válida. Para desenvolvimento local, gere uma chave com `openssl rand -hex 32`, coloque o resultado apenas em `.env` e ajuste `ACCESS_TOKEN_EXPIRE_MINUTES` se necessário. Não compartilhe nem versione `.env`.

O limite de upload é 10 MiB por padrão. `UploadFile` mantém uploads grandes em arquivo temporário em vez de carregá-los integralmente na memória; a rota verifica o tamanho informado e lê no máximo limite + 1 bytes antes de processar. O serviço de PDF existente também aplica seu limite.

A rota consulta `PurchaseOrderRepository`; em execução normal, `SqlAlchemyPurchaseOrderRepository` usa as tabelas PostgreSQL. Os testes injetam um repositório SQLite independente e não dependem do `DATABASE_URL` local. `DocumentProcessingService` e `ExtractionProvider` também continuam substituíveis. O app padrão usa `OllamaExtractionProvider`; configure o modelo com `DOCUFLOW_OLLAMA_MODEL` (padrão `gemma4`). Os testes substituem o provedor por um stub e não chamam Ollama.

## PostgreSQL e migrações

O desenvolvimento local usa a imagem oficial `postgres:18.6-bookworm`, com versão fixada, healthcheck e o volume nomeado `docuflow_local_postgres_data`. O Compose cria `docuflow` para a aplicação e executa `docker/postgres/init-test-db.sql` para criar `docuflow_test` na inicialização inicial do volume.

Copie o exemplo local e inicie somente o serviço PostgreSQL:

```bash
cp .env.example .env
docker compose up -d postgres
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

O seed pode ser executado mais de uma vez: se o pedido `PC-2026-0001` já existir, ele informa isso e não o duplica. A migração `20261013_0002` cria o workspace `demo` e associa a ele todos os fornecedores e pedidos já existentes, preservando seus IDs, itens e demais dados. A migração não apaga registros. Fornecedores e pedidos passam a ter chaves e unicidade com escopo do workspace, e uma chave estrangeira composta impede que um pedido use fornecedor de outro workspace. Itens continuam vinculados ao pedido e herdam esse escopo. A aplicação não cria tabelas nem dados durante a inicialização. Os modelos ficam em `backend/app/models/`; as revisões ficam em `backend/migrations/versions/`. Valores de quantidade e dinheiro usam `NUMERIC` no banco e `Decimal` em Python. As credenciais não devem ser adicionadas ao arquivo Alembic nem ao código.

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

Os testes unitários atuais continuam usando SQLite temporário e não leem `DATABASE_URL`:

```bash
./.venv/bin/python -m unittest discover -s backend/tests -v
```

Os testes de integração rodam separadamente contra o PostgreSQL local e exigem `TEST_DATABASE_URL` apontando para `localhost` e para o banco dedicado `docuflow_test`. Eles aplicam apenas `upgrade head`, não removem tabelas nem limpam dados; os registros criados pelos testes permanecem nessa base de teste. O helper também recusa SQLite, outros nomes de banco e uma URL igual a `DATABASE_URL`.

```bash
./.venv/bin/python -m unittest discover -s backend/tests/integration -v
```

O SQL de inicialização do Compose cria a base de teste somente quando o volume PostgreSQL é inicializado pela primeira vez. Se estiver usando um volume local existente no qual essa base ainda não existe, crie-a sem apagar o volume com `docker compose exec postgres createdb -U docuflow docuflow_test` (ajuste o usuário se mudou `POSTGRES_USER`). Para parar o serviço mantendo todos os dados, use `docker compose stop postgres`; para iniciá-lo novamente, `docker compose start postgres`. Não use `down -v` para encerrar o ambiente.

## Extração estruturada

O serviço `StructuredExtractionService` recebe um provedor pela interface `ExtractionProvider`; o adaptador `OllamaExtractionProvider` usa `POST /api/chat` localmente e envia o schema de `ReferenceDocument` no campo `format`. Configure o nome do modelo e o timeout em `OllamaConfig`; `StructuredExtractionConfig.max_attempts` limita as tentativas de comunicação (padrão: 2). Os testes simulam o provedor e não exigem Ollama em execução.

`DocumentProcessingService` compõe o extrator de PDF, o serviço estruturado e a validação comercial. Ele devolve o documento, avisos do PDF, relatório de validação e estado final; erros das etapas são propagados sem incluir os bytes do arquivo no resultado.
