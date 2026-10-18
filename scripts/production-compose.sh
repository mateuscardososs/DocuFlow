#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(CDPATH= cd -- "$script_dir/.." && pwd)
env_file=${DOCUFLOW_PRODUCTION_ENV_FILE:-$repo_root/.env.production}

if [ ! -f "$env_file" ]; then
	printf '%s\n' "Arquivo de ambiente de produção não encontrado. Copie deploy/production.env.example para .env.production e preencha os valores reais." >&2
	exit 2
fi

python3 "$script_dir/validate_production_env.py" "$env_file"
exec docker compose \
	--env-file "$env_file" \
	--file "$repo_root/compose.production.yaml" \
	"$@"
