# Backup, restauração e recuperação

Procedimentos destinados à instalação de piloto em VPS. Confirme cuidadosamente o projeto Compose, banco e nomes dos volumes antes de executar. Não use esses comandos no ambiente local nem contra dados que não pertençam à instalação pretendida.

## Backup, restauração e recuperação de workers

Para um backup consistente entre metadados, PDFs e jobs, entre em manutenção e interrompa proxy, API e os dois workers para impedir novas alterações. Depois pare Redis para congelar seu AOF enquanto o dump PostgreSQL e os volumes são copiados. PostgreSQL é salvo com `pg_dump` em formato custom; os volumes privados de documentos, Redis e dados de TLS do Caddy são arquivados somente para leitura. Trate o backup Caddy como material sensível, pois contém certificados e chaves privadas. Use um destino de backup fora do diretório do projeto, com criptografia e retenção apropriadas:

```bash
(
  set -eu
  umask 077
  BACKUP_DIR=/srv/backups/docuflow/$(date -u +%Y%m%dT%H%M%SZ)
  mkdir -p "$BACKUP_DIR"
  restart_docuflow() {
    ./scripts/production-compose.sh up -d --wait redis api document-worker erp-worker frontend proxy
  }
  trap restart_docuflow EXIT
  ./scripts/production-compose.sh stop proxy api document-worker erp-worker
  ./scripts/production-compose.sh stop redis
  ./scripts/production-compose.sh exec -T postgres sh -lc \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$BACKUP_DIR/postgres.dump"
  docker volume inspect docuflow_production_private_documents docuflow_production_redis_data docuflow_production_caddy_data >/dev/null
  docker run --rm --network none \
    -v docuflow_production_private_documents:/source:ro \
    -v "$BACKUP_DIR:/backup" alpine:3.22 \
    sh -c 'umask 077; tar -C /source -czf /backup/documents.tar.gz .'
  docker run --rm --network none \
    -v docuflow_production_redis_data:/source:ro \
    -v "$BACKUP_DIR:/backup" alpine:3.22 \
    sh -c 'umask 077; tar -C /source -czf /backup/redis.tar.gz .'
  docker run --rm --network none \
    -v docuflow_production_caddy_data:/source:ro \
    -v "$BACKUP_DIR:/backup" alpine:3.22 \
    sh -c 'umask 077; tar -C /source -czf /backup/caddy-data.tar.gz .'
  sha256sum "$BACKUP_DIR/postgres.dump" "$BACKUP_DIR/documents.tar.gz" "$BACKUP_DIR/redis.tar.gz" "$BACKUP_DIR/caddy-data.tar.gz" > "$BACKUP_DIR/SHA256SUMS"
  restart_docuflow
  trap - EXIT
  printf 'Backup salvo em %s\\n' "$BACKUP_DIR"
)
```

Os comandos usam os nomes de volume padrão. Se `DOCUFLOW_DOCUMENTS_VOLUME`, `DOCUFLOW_REDIS_VOLUME` ou `DOCUFLOW_CADDY_DATA_VOLUME` foram personalizados, substitua os nomes correspondentes pelo nome real antes de executar. Faça cópias dos quatro artefatos e dos checksums para armazenamento protegido independente da VPS. Mantenha também os valores de `.env.production` no gestor de segredos para recuperar credenciais e a chave JWT; não os coloque em claro junto dos dumps. Reinicie os serviços parados após o backup. Não é suficiente copiar apenas PostgreSQL ou apenas os PDFs: registros, arquivos e fila devem pertencer ao mesmo ponto de consistência.

Teste restauração periodicamente em uma VPS/instalação isolada, com nome de projeto, volumes e banco diferentes; nunca restaure sobre a produção para fazer um ensaio. Use o caminho de backup impresso pela etapa anterior. Um ensaio básico pode restaurar PostgreSQL e arquivos em volumes separados, sem publicar portas nem conectar à rede da produção (substitua o caminho, `POSTGRES_USER` e `POSTGRES_DB` pelos valores configurados; a senha abaixo é descartável e só existe no contêiner isolado):

```bash
BACKUP_DIR=/srv/backups/docuflow/20261018T120000Z
sha256sum -c "$BACKUP_DIR/SHA256SUMS"
RESTORE_NAME="docuflow-restore-$(date -u +%Y%m%dT%H%M%SZ)"
RESTORE_DOCS_VOLUME="${RESTORE_NAME}-documents"
RESTORE_REDIS_VOLUME="${RESTORE_NAME}-redis"
RESTORE_CADDY_VOLUME="${RESTORE_NAME}-caddy-data"
docker volume create "$RESTORE_NAME"
docker volume create "$RESTORE_DOCS_VOLUME"
docker volume create "$RESTORE_REDIS_VOLUME"
docker volume create "$RESTORE_CADDY_VOLUME"
docker run -d --name "$RESTORE_NAME" --network none \
  -e POSTGRES_DB=docuflow -e POSTGRES_USER=docuflow \
  -e POSTGRES_PASSWORD=restore-only-not-a-secret \
  -v "$RESTORE_NAME:/var/lib/postgresql" postgres:18.6-bookworm
until docker exec "$RESTORE_NAME" pg_isready -U docuflow -d docuflow; do sleep 2; done
docker exec -i "$RESTORE_NAME" sh -lc \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner' < "$BACKUP_DIR/postgres.dump"
docker run --rm --network none \
  -v "$RESTORE_DOCS_VOLUME:/restore" \
  -v "$BACKUP_DIR/documents.tar.gz:/backup/documents.tar.gz:ro" alpine:3.22 \
  sh -c 'tar -C /restore -xzf /backup/documents.tar.gz'
docker run --rm --network none \
  -v "$RESTORE_REDIS_VOLUME:/restore" \
  -v "$BACKUP_DIR/redis.tar.gz:/backup/redis.tar.gz:ro" alpine:3.22 \
  sh -c 'tar -C /restore -xzf /backup/redis.tar.gz'
docker run --rm --network none \
  -v "$RESTORE_CADDY_VOLUME:/restore" \
  -v "$BACKUP_DIR/caddy-data.tar.gz:/backup/caddy-data.tar.gz:ro" alpine:3.22 \
  sh -c 'tar -C /restore -xzf /backup/caddy-data.tar.gz'
docker exec "$RESTORE_NAME" psql -U docuflow -d docuflow -c 'SELECT version_num FROM alembic_version'
```

Para uma verificação funcional completa, configure um Compose de restauração com outro nome de projeto, banco e volumes apontando para esses dados (inclusive Redis); compare a revisão Alembic, execute healthchecks e confira com uma conta de teste que um documento conhecido pode ser consultado e baixado e que jobs pendentes voltam a ser consumidos. Não exponha o proxy restaurado durante o ensaio. Registre o tempo e o resultado; mantenha a produção sem alterações durante o ensaio. Os comandos isolados acima deixam um container e quatro volumes de teste para inspeção: remova-os apenas depois de conferir que os nomes correspondem ao ensaio, sem usar comandos globais de limpeza.

Se worker cair, primeiro confirme a saúde de PostgreSQL e Redis e inicie os serviços novamente. Verifique a fila e os documentos com estado `processing`; depois execute o recuperador documental com o projeto/ambiente de produção carregado. O comando examina documentos antigos em processamento e recoloca os elegíveis na fila; não é uma garantia de execução exatamente uma vez. Faça isso somente para esta instalação, nunca contra outra base ou ambiente:

```bash
./scripts/production-compose.sh run --rm --no-deps api \
  python -m backend.app.recover_documents
./scripts/production-compose.sh logs --tail=100 document-worker erp-worker
```

O identificador fictício `ERP-DEMO-...` e os estados de solicitações não correspondem a lançamentos em um ERP real. Nenhuma rotina de reconciliação externa está implementada; após qualquer operação de restauração, confira manualmente o estado do sistema simulado e não suponha sincronização com um ERP.
