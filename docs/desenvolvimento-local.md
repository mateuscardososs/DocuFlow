# Desenvolvimento local

Instruções para rodar o DocuFlow em ambiente local. Elas não se aplicam à VPS; veja [implantação em VPS](implantacao-vps.md).

## Dependências do backend

`pypdf` lê texto nativo de PDFs. Para páginas sem texto utilizável, `pypdfium2` renderiza a página localmente e o executável Tesseract faz OCR; Pillow valida e normaliza PNG/JPEG. O OCR não usa serviços externos e sua saída ainda passa pelas mesmas verificações de evidência e validação comercial. `pypdfium2` 5.14.0 e Pillow 12.3.0 foram instalados no Python 3.14.4 do projeto; os metadados oficiais declaram suporte a Python 3.14 ([pypdfium2 no PyPI](https://pypi.org/project/pypdfium2/), [Pillow no PyPI](https://pypi.org/project/pillow/)). A imagem do worker documental instala Tesseract e os dados `por` e `eng`; para executar o worker no host, instale o mecanismo e os idiomas ([instalação oficial](https://tesseract-ocr.github.io/tessdoc/Installation.html), [dados de idioma](https://tesseract-ocr.github.io/tessdoc/Data-Files.html)). No macOS use `brew install tesseract tesseract-lang`; em Debian/Ubuntu, `sudo apt install tesseract-ocr tesseract-ocr-por tesseract-ocr-eng`. A integração usa a CLI documentada pelo Tesseract ([uso da linha de comando](https://tesseract-ocr.github.io/tessdoc/Command-Line-Usage.html)). As demais bibliotecas servem à API, autenticação, PostgreSQL e fila; RQ 2.12.0 e redis-py 8.1.0 também foram instalados e verificados em Python 3.14 ([documentação RQ](https://python-rq.org/docs/), [documentação redis-py](https://redis.readthedocs.io/en/stable/)). Para instalar as dependências Python e rodar os testes:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install -r requirements-samples.txt
.venv/bin/python -m unittest discover -s backend/tests -v
```

## PostgreSQL e migrações

O desenvolvimento local usa `postgres:18.6-bookworm` e `redis:8.2.10-alpine3.22`, com versões fixadas, healthchecks e volumes nomeados `docuflow_local_postgres_data` e `docuflow_local_redis_data`. O Compose cria `docuflow` para a aplicação e executa `docker/postgres/init-test-db.sql` para criar `docuflow_test` na inicialização inicial do volume.

Se ainda não existir `.env`, copie o exemplo local. Não substitua uma configuração local já existente. Inicie PostgreSQL e Redis:

```bash
if [ ! -f .env ]; then cp .env.example .env; fi
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

O teste Playwright pode manter API e os dois workers em containers enquanto usa uma fila Redis e a base PostgreSQL dedicadas. Ele não inicia nem encerra PostgreSQL/Redis, e usa uma rede externa já criada pelo Compose. Para habilitá-lo, siga [o guia de testes](testes.md), instale Chromium, carregue `.env` e confirme que `docuflow_test` e Redis `/15` estão disponíveis.

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

## Execução pelo host (opcional)

O modo Compose acima é o caminho rápido. Para executar API e workers diretamente no host, mantenha PostgreSQL e Redis do Compose em execução, carregue o `.env` no terminal sem imprimir seu conteúdo e rode as migrações e o seed explicitamente:

```bash
set -a
. ./.env
set +a
.venv/bin/alembic -c backend/alembic.ini upgrade head
.venv/bin/python -m backend.app.seed_reference_order
.venv/bin/uvicorn backend.app.main:app --reload
```

Em dois terminais adicionais, inicie cada worker. O primeiro consome documentos e requer Tesseract com `por` e `eng`; o segundo consome somente a fila simulada do ERP:

```bash
set -a
. ./.env
set +a
.venv/bin/rq worker --url "$REDIS_URL" "$DOCUFLOW_QUEUE_NAME"
```

```bash
set -a
. ./.env
set +a
.venv/bin/rq worker --url "${ERP_REDIS_URL:-$REDIS_URL}" "${DOCUFLOW_ERP_QUEUE_NAME:-erp-submissions}"
```

Em outro terminal, inicie a interface pelo host:

```bash
cd frontend
npm ci
npm run dev
```

A interface usa `http://localhost:5173` e a API local `http://localhost:8000`. CORS aceita somente a origem configurada. O worker documental usa Ollama conforme `DOCUFLOW_OLLAMA_BASE_URL`; em Compose local, o exemplo aponta para `host.docker.internal`, adequado apenas a esse modo. Em produção, use a URL privada documentada em [implantação em VPS](implantacao-vps.md).

## Parar sem remover dados

Para pausar PostgreSQL e Redis do Compose sem apagar os volumes, use `docker compose stop postgres redis`; para voltar a iniciá-los, `docker compose start postgres redis`. Não use `docker compose down -v` neste ambiente.

As regras de processamento, limites, estados e permissões estão em [funcionalidades e contratos](funcionalidades.md).
