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

`pypdf` lê o texto nativo dos PDFs localmente, sem OCR nem dependências de sistema. `FastAPI` expõe as rotas; `python-multipart` analisa formulários com arquivos; `httpx` atende ao `TestClient`; `uvicorn` inicia o servidor ASGI; SQLAlchemy e Psycopg conectam ao PostgreSQL; e Alembic aplica migrações. As versões instaladas e verificadas no ambiente Python 3.14 são FastAPI 0.143.0, python-multipart 0.0.32, httpx 0.28.1, Uvicorn 0.54.0, SQLAlchemy 2.1.4, Alembic 1.20.0 e Psycopg 3.3.6. As versões atuais declaram suporte ao Python 3.14 nas informações oficiais dos projetos ([SQLAlchemy](https://pypi.org/project/SQLAlchemy/), [Alembic](https://pypi.org/project/alembic/), [Psycopg](https://pypi.org/project/psycopg/)). Para instalar as dependências e rodar os testes:

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

`GET /health` retorna `{"status":"ok"}`. `POST /documents/analyze` recebe um formulário multipart com `file` (`application/pdf`) e `purchase_order_reference` (por exemplo, `PC-2026-0001`); retorna o documento extraído, avisos do PDF, achados da validação comercial e o estado `blocked`, `divergent` ou `ready_for_review`. Uma divergência retorna HTTP 200 com os achados no corpo e nunca aprova nem lança o documento. Erros de tipo, tamanho, PDF inválido e pedido ausente retornam respostas HTTP 4xx; falha ou timeout do provedor retorna 502 ou 504. Erros inesperados retornam uma mensagem genérica sem detalhes internos.

O limite de upload é 10 MiB por padrão. `UploadFile` mantém uploads grandes em arquivo temporário em vez de carregá-los integralmente na memória; a rota verifica o tamanho informado e lê no máximo limite + 1 bytes antes de processar. O serviço de PDF existente também aplica seu limite.

A rota consulta `PurchaseOrderRepository`; em execução normal, `SqlAlchemyPurchaseOrderRepository` usa as tabelas PostgreSQL. Os testes injetam um repositório SQLite independente e não dependem do `DATABASE_URL` local. `DocumentProcessingService` e `ExtractionProvider` também continuam substituíveis. O app padrão usa `OllamaExtractionProvider`; configure o modelo com `DOCUFLOW_OLLAMA_MODEL` (padrão `gemma4`). Os testes substituem o provedor por um stub e não chamam Ollama.

## PostgreSQL e migrações

Defina a URL fora do código e do repositório, por exemplo em uma variável de ambiente local:

```bash
export DATABASE_URL='postgresql+psycopg://USUARIO:SENHA@localhost:5432/docuflow'
```

O banco precisa existir antes de aplicar a migração. Crie as tabelas com Alembic e cadastre o caso de demonstração em um comando explícito:

```bash
.venv/bin/alembic -c backend/alembic.ini upgrade head
.venv/bin/python -m backend.app.seed_reference_order
.venv/bin/uvicorn backend.app.main:app --reload
```

O seed pode ser executado mais de uma vez: se o pedido `PC-2026-0001` já existir, ele informa isso e não o duplica. A aplicação não cria tabelas nem dados durante a inicialização. Os modelos ficam em `backend/app/models/`; a revisão inicial está em `backend/migrations/versions/`. Valores de quantidade e dinheiro usam `NUMERIC` no banco e `Decimal` em Python. As credenciais não devem ser adicionadas ao arquivo Alembic nem ao código.

Os testes de persistência e da migração rodam em SQLite temporário e isolado; a suíte não precisa de um PostgreSQL disponível nem usa a URL configurada para o ambiente local. A execução da migração e dos testes contra um servidor PostgreSQL ainda deve ser feita ao preparar esse banco local.

## Extração estruturada

O serviço `StructuredExtractionService` recebe um provedor pela interface `ExtractionProvider`; o adaptador `OllamaExtractionProvider` usa `POST /api/chat` localmente e envia o schema de `ReferenceDocument` no campo `format`. Configure o nome do modelo e o timeout em `OllamaConfig`; `StructuredExtractionConfig.max_attempts` limita as tentativas de comunicação (padrão: 2). Os testes simulam o provedor e não exigem Ollama em execução.

`DocumentProcessingService` compõe o extrator de PDF, o serviço estruturado e a validação comercial. Ele devolve o documento, avisos do PDF, relatório de validação e estado final; erros das etapas são propagados sem incluir os bytes do arquivo no resultado.
