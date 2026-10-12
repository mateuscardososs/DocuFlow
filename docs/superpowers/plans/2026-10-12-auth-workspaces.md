# Autenticação e isolamento por workspace — plano de implementação

> Execução inline nesta sessão, autorizada pelo usuário. Sem commits e sem subagentes.

**Objetivo:** adicionar autenticação JWT, hashes Argon2, papéis por workspace e isolamento de fornecedores e pedidos, preservando os registros PostgreSQL existentes.

**Arquitetura:** `User` armazena e-mail e hash Argon2; `Workspace` representa o limite de dados; `WorkspaceMembership` associa usuário, workspace e um papel por linha. Um JWT identifica o usuário. `X-Workspace-ID` seleciona o workspace em cada análise, e o backend valida membership e papel antes de consultar o pedido.

**Tecnologias:** FastAPI, SQLAlchemy, Alembic, `pwdlib[argon2]`, PyJWT e unittest; sem serviços externos.

## Restrições globais

- Exigir `JWT_SECRET_KEY` do ambiente, sem segredo padrão; validade configurável.
- Guardar senhas somente em hash Argon2id.
- Permitir `operator` e `approver` por associação; dois registros permitem ambos.
- Escopar pedidos e fornecedores por workspace e não revelar pedidos de outro workspace.
- Preservar todos os registros já existentes ao migrar para o workspace `demo`.
- Não remover volumes, bancos, nem depender de Ollama.
- Solicitar senha de administração com prompt oculto, nunca em argumento ou arquivo versionado.

---

### Etapa 1: testes para contratos de autenticação e isolamento

**Arquivos:** `backend/tests/test_api.py`, `backend/tests/test_purchase_order_repository.py`, `backend/tests/integration/test_postgres_integration.py`.

- Adicionar testes para login, senha inválida, bearer inválido/expirado, rota sem token, papel insuficiente, operador autorizado, dois papéis e isolamento entre workspaces.
- Atualizar fixtures SQLite para criar workspace demo, membership e tokens de teste explícitos.
- Adicionar integração PostgreSQL verificando que a revisão nova atribui todos os registros legados ao workspace `demo` e mantém fornecedores, pedidos e itens consultáveis.
- Rodar os testes alterados antes da implementação e confirmar falhas por contratos ausentes.

### Etapa 2: modelos, repositórios e migração

**Arquivos:** `backend/app/models/identity.py`, `backend/app/models/purchase_order.py`, `backend/app/models/__init__.py`, `backend/app/repositories/purchase_orders.py`, `backend/migrations/versions/20261013_0002_auth_workspaces.py`.

- Criar tabelas `users`, `workspaces`, `workspace_memberships`; membership usa PK `(user_id, workspace_id, role)` e CHECK para os dois papéis.
- Adicionar workspace a fornecedores e pedidos; criar unicidades compostas e FK composta para garantir que o fornecedor do pedido pertence ao mesmo workspace.
- Na revisão Alembic, criar `demo`, adicionar colunas nullable, backfill dos fornecedores e pedidos, depois torná-las NOT NULL e aplicar FKs/constraints. Não remover nem recriar tabelas existentes.
- Alterar `PurchaseOrderRepository.get_by_reference(reference, workspace_id)` e exigir filtro de workspace no SQLAlchemy.
- Validar a migração em PostgreSQL e SQLite.

### Etapa 3: serviço de autenticação e proteção da API

**Arquivos:** `backend/app/auth.py`, `backend/app/api/app.py`, `backend/app/schemas.py`, `requirements.txt`, `.env.example`, testes API.

- Hash/verify com `PasswordHash.recommended()` e PyJWT HS256; verificar `exp` e `sub`, fixar o algoritmo aceito e responder 401 genérico com `WWW-Authenticate`.
- Adicionar `POST /auth/token` como formulário OAuth2, `GET /auth/me` com memberships/papéis, e dependências para usuário autenticado, workspace membro e papel operator.
- Proteger `/documents/analyze`; validar workspace e operador antes de buscar o pedido. Retornar 404 para referência inexistente dentro daquele workspace.
- Criar configuração a partir de `JWT_SECRET_KEY` e `ACCESS_TOKEN_EXPIRE_MINUTES`; falhar sem chave configurada.
- Fixar faixas de versões das dependências após validar Python 3.14.

### Etapa 4: comandos administrativos e documentação

**Arquivos:** `backend/app/admin.py`, `README.md`, `.env.example`.

- Implementar `create-workspace` e `create-user`; a senha vem de `getpass`, confirmação e validação mínima, não de argumento.
- Permitir várias opções `--role` e associação a workspace existente.
- Documentar geração da chave JWT, migração, primeiro workspace e primeiro operador/aprovador.

### Etapa 5: validação final

- Rodar `./.venv/bin/python -m unittest discover -s backend/tests -v`.
- Carregar `.env` e rodar `./.venv/bin/python -m unittest discover -s backend/tests/integration -v` contra PostgreSQL Compose.
- Confirmar healthcheck, `git diff --check`, ausência de `.env` versionado e integridade de dados legados.
