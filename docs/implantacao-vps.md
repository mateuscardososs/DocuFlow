# Implantação de piloto em VPS

Esta configuração é separada do Compose local. Ela descreve uma proposta reproduzível de implantação em host único; não comprova deploy público nem operação com domínio real. Antes de operar com dados reais, valide DNS, firewall, certificados, segredos, conectividade privada do provedor e restauração no provedor escolhido.

## Piloto em uma VPS

`compose.production.yaml` é uma configuração independente para o piloto. Ela não estende nem altera `compose.yaml`, os nomes dos volumes locais ou `.env`. O script `scripts/production-compose.sh` valida o arquivo de ambiente antes de cada comando Compose. O exemplo `deploy/production.env.example` contém apenas valores ilustrativos e é rejeitado pelo validador; `.env.production` é ignorado pelo Git.

### Pré-requisitos e configuração inicial

Use uma VPS Linux com Docker Engine e o plugin Docker Compose instalados, espaço e rotina de backup apropriados ao volume de documentos, e um domínio controlado pelo operador. Aponte o registro DNS A (e AAAA, se usado) para a VPS, libere TCP 80 e 443 no firewall da hospedagem e no firewall do sistema. Caddy solicita e renova certificados automaticamente; isso só será validado quando o domínio real resolver para o host e esses ports estiverem acessíveis. Não há domínio ou provedor de hospedagem fixado neste repositório.

Na VPS, clone uma versão revisada do projeto. Copie o exemplo para um arquivo privado fora do controle de versão e edite os valores. Use um gestor de segredos para criar valores aleatórios; não os passe em argumentos de comandos, não os publique em tickets e restrinja a leitura do arquivo ao usuário de deploy:

```bash
cp deploy/production.env.example .env.production
chmod 600 .env.production
${EDITOR:-vi} .env.production
./scripts/production-compose.sh config --quiet
```

Substitua domínio, e-mail ACME, usuário/base do PostgreSQL, senha do PostgreSQL, senha do Redis, chave JWT, modelo e nomes necessários. O validador recusa campos ausentes ou ilustrativos, segredos curtos/inseguros, identificadores de banco inválidos, host local e URL pública para Ollama. Segredos de banco/Redis precisam ter ao menos 32 caracteres URL-safe; `JWT_SECRET_KEY`, ao menos 64. A senha URL-safe evita que a URL interna do PostgreSQL precise de codificação adicional. Mantenha o mesmo segredo e os mesmos nomes de volumes durante atualizações. `.env.production` não deve ser copiado para imagens ou para o repositório.

O Ollama não é incluído no Compose de produção e o modelo não é baixado durante o build. O Compose cria `ollama_private` como rede interna quando o worker documental é iniciado. Depois de migração e seed, inicie esse worker para criar a rede antes de provisionar o provedor:

```bash
./scripts/production-compose.sh up -d --wait document-worker
docker network inspect --format '{{.Internal}}' docuflow_ollama_private
```

O segundo comando deve mostrar `true`. Substitua o nome pelo valor de `DOCUFLOW_OLLAMA_DOCKER_NETWORK` se tiver configurado outro. Provisionar o container Ollama fica a cargo do operador. Ele deve usar essa rede como sua única rede, sem `ports:`/`-p` e com o modelo já disponível em armazenamento persistente. Não conecte o container a redes públicas nem publique a porta 11434. Configure `DOCUFLOW_OLLAMA_BASE_URL=http://ollama:11434` e o modelo disponível; dê ao container o alias `ollama` nessa rede interna. Se o provedor estiver em outro host, esta configuração não cria uma rota para ele; conectividade privada remota exigiria uma rede/VPN e regras de roteamento revisadas separadamente. O validador bloqueia host público, `host.docker.internal`, localhost e loopback. A API fica apenas na rede interna da aplicação; somente o worker documental também entra na rede interna do Ollama.

### Deploy, atualização e rollback

Confirme o arquivo antes do primeiro deploy. Construa as imagens localmente na VPS (o build não chama Ollama nem baixa modelos), inicie os serviços de dados e espere os healthchecks, faça backup antes de migrar e aplique Alembic explicitamente. A aplicação não migra automaticamente ao iniciar:

```bash
./scripts/production-compose.sh config --quiet
./scripts/production-compose.sh build api document-worker erp-worker frontend
./scripts/production-compose.sh up -d --wait postgres redis
./scripts/production-compose.sh run --rm --no-deps api alembic -c backend/alembic.ini upgrade head
./scripts/production-compose.sh run --rm --no-deps api python -m backend.app.seed_reference_order
./scripts/production-compose.sh up -d --wait document-worker
# Provisione o Ollama somente na rede interna criada acima; confirme o healthcheck do worker.
./scripts/production-compose.sh up -d --wait api erp-worker frontend proxy
./scripts/production-compose.sh ps
```

O seed é explícito e idempotente para o pedido fictício. Crie o primeiro usuário só depois das migrações, usando o prompt de senha oculto (não informe senha como argumento):

```bash
./scripts/production-compose.sh run --rm --no-deps api \
  python -m backend.app.admin create-user \
  --email operador@seu-dominio-configurado --workspace demo --role operator --role approver
```

Antes de executar, substitua o endereço ilustrativo pelo e-mail real do operador.

O reverse proxy é o único serviço que publica portas: 80 para redirecionamento/ACME e 443 para HTTPS. PostgreSQL, Redis, API, workers e frontend não têm portas publicadas; o frontend estático é acessível pelo proxy, e usa como base da API `https://<DOCUFLOW_DOMAIN>`. A API e o worker documental compartilham o volume privado; o frontend nunca monta documentos. Use `docker compose logs` com cuidado: não imprima `.env.production` nem habilite rastreamento de ambiente.

Para atualizar, obtenha uma versão/tag revisada, faça e verifique backup consistente, construa as imagens da versão e aplique as migrações explicitamente antes de recriar os serviços. Depois verifique healthchecks, login, upload e download de um documento de teste. Não use `down -v`, não altere nomes de volumes e não rode migração contra uma base cuja identidade não foi confirmada.

Para rollback de aplicação, volte ao commit/tag anterior e reconstrua/recrie os containers. Só faça isso se o esquema resultante continuar compatível com a versão anterior. Não tente “desfazer” uma migração destrutiva manualmente. Se a versão anterior não for compatível, coloque o serviço em manutenção e restaure em conjunto PostgreSQL, documentos e Redis do mesmo ponto de backup, preferencialmente primeiro em uma instalação isolada. Redis guarda jobs RQ; sem restaurar sua persistência, documentos `queued` podem ficar sem job, pois o recuperador atual trata documentos antigos em `processing`. Após a restauração, recupere documentos presos conforme abaixo e confira solicitações ERP pendentes antes de reprocessá-las.

### Validação local desta configuração

O verificador cria arquivos temporários com credenciais fictícias, renderiza os dois modelos com `docker compose config` e confere as portas e redes declaradas sem iniciar containers:

```bash
python3 -m unittest backend.tests.test_production_environment -v
python3 scripts/check_compose_configs.py
```

Para validar a configuração de produção manualmente, preencha uma cópia privada válida de `deploy/production.env.example` e rode `./scripts/production-compose.sh config --quiet`. O exemplo versionado deve falhar deliberadamente por conter placeholders. Essas verificações estáticas não validam emissão/renovação real do certificado, firewall, DNS, conectividade do Ollama, desempenho, recuperação de backup nem operação de uma VPS. O piloto precisa de validação no domínio e provedor escolhidos antes de receber dados reais.


Para o procedimento de cópia consistente de PostgreSQL, PDFs, Redis e dados TLS do Caddy, consulte [backup e restauração](backup-restauracao.md).
