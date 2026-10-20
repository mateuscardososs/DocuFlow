# Testes e validação

Os comandos abaixo reproduzem as suítes configuradas em [GitHub Actions](https://github.com/mateuscardososs/DocuFlow/actions). Use configurações dedicadas para PostgreSQL e Redis nos testes de integração; não aponte `TEST_DATABASE_URL` para a base normal nem `TEST_REDIS_URL` para a fila de desenvolvimento.

## Testes backend

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
.venv/bin/python -m pip install -r requirements-samples.txt
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
cd "$(git rev-parse --show-toplevel)"
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

## Escopo de validação

Os testes Playwright automatizam a interface do DocuFlow com navegador Chromium, API, workers e banco/fila de teste. Eles usam provedor estruturado determinístico e `DemoERPClient`. Isso verifica os fluxos da aplicação, não interação com ERP externo, emissão fiscal, RPA ou reconciliação com um sistema após lançamento. A avaliação com modelo real é manual e separada desta suíte; seus relatórios ficam em `var/evaluations/`, ignorado pelo Git. A amostra não constitui benchmark nem taxa de acerto geral.
