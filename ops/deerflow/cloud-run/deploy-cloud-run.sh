#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Deploy DeerFlow frontend/backend to Cloud Run for Orpheus native workspace mode.

Usage:
  ops/deerflow/cloud-run/deploy-cloud-run.sh [all|build|deploy]

Required:
  GOOGLE_CLOUD_PROJECT or PROJECT_ID

Common environment variables:
  CLOUD_RUN_REGION                         default: us-central1
  ARTIFACT_REGISTRY_REPOSITORY             default: deerflow
  IMAGE_TAG                                default: current git sha
  DEERFLOW_BACKEND_SERVICE                 default: deerflow-gateway
  DEERFLOW_FRONTEND_SERVICE                default: deerflow-frontend
  DEERFLOW_BACKEND_ALLOW_UNAUTHENTICATED   default: 1
  DEERFLOW_FRONTEND_ALLOW_UNAUTHENTICATED  default: 1
  DEERFLOW_FRONTEND_PUBLIC_URL             optional, used for CORS and public links
  ORPHEUS_AGENT_WORKSPACE_CALLBACK_URL     optional Orpheus callback endpoint

Secret Manager names:
  DATABASE_URL_SECRET_NAME                 default: deerflow-database-url
  VERCEL_TOKEN_SECRET_NAME                 default: deerflow-vercel-token
  VERCEL_PROJECT_ID_SECRET_NAME            default: deerflow-vercel-project-id
  VERCEL_TEAM_ID_SECRET_NAME               optional
  OPENAI_API_KEY_SECRET_NAME               optional, required for the default model
  BETTER_AUTH_SECRET_NAME                  default: deerflow-better-auth-secret
  AUTH_JWT_SECRET_SECRET_NAME              default: deerflow-auth-jwt-secret
  DEER_FLOW_INTERNAL_AUTH_TOKEN_SECRET_NAME default: deerflow-internal-auth-token
  DEERFLOW_EMBED_TOKEN_SECRET_NAME         optional, required for Orpheus signed embed auth
  ORPHEUS_AGENT_WORKSPACE_CALLBACK_TOKEN_SECRET_NAME optional

The script references Secret Manager values; it never writes secret values to
the repository or to gcloud command history.
USAGE
}

die() {
  echo "error: $*" >&2
  exit 1
}

join_by_comma() {
  local IFS=,
  echo "$*"
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../../.." && pwd)"
cd "$repo_root"

command="${1:-all}"
case "$command" in
  all|build|deploy) ;;
  -h|--help|help)
    usage
    exit 0
    ;;
  *)
    usage >&2
    die "unknown command: $command"
    ;;
esac

require_command gcloud
require_command git

project_id="${PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-}}"
if [[ -z "$project_id" ]]; then
  project_id="$(gcloud config get-value project 2>/dev/null || true)"
fi
[[ -n "$project_id" && "$project_id" != "(unset)" ]] || die "set PROJECT_ID or GOOGLE_CLOUD_PROJECT"

region="${CLOUD_RUN_REGION:-us-central1}"
repository="${ARTIFACT_REGISTRY_REPOSITORY:-deerflow}"
backend_service="${DEERFLOW_BACKEND_SERVICE:-deerflow-gateway}"
frontend_service="${DEERFLOW_FRONTEND_SERVICE:-deerflow-frontend}"
image_tag="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
registry_host="${region}-docker.pkg.dev"
image_prefix="${registry_host}/${project_id}/${repository}"
backend_image="${DEERFLOW_BACKEND_IMAGE:-${image_prefix}/${backend_service}:${image_tag}}"
frontend_image="${DEERFLOW_FRONTEND_IMAGE:-${image_prefix}/${frontend_service}:${image_tag}}"

backend_cpu="${DEERFLOW_BACKEND_CPU:-1}"
backend_memory="${DEERFLOW_BACKEND_MEMORY:-2Gi}"
backend_concurrency="${DEERFLOW_BACKEND_CONCURRENCY:-10}"
backend_min_instances="${DEERFLOW_BACKEND_MIN_INSTANCES:-1}"
backend_max_instances="${DEERFLOW_BACKEND_MAX_INSTANCES:-2}"
backend_timeout="${DEERFLOW_BACKEND_TIMEOUT:-3600}"
backend_allow_unauthenticated="${DEERFLOW_BACKEND_ALLOW_UNAUTHENTICATED:-1}"

frontend_cpu="${DEERFLOW_FRONTEND_CPU:-1}"
frontend_memory="${DEERFLOW_FRONTEND_MEMORY:-2Gi}"
frontend_concurrency="${DEERFLOW_FRONTEND_CONCURRENCY:-80}"
frontend_min_instances="${DEERFLOW_FRONTEND_MIN_INSTANCES:-0}"
frontend_max_instances="${DEERFLOW_FRONTEND_MAX_INSTANCES:-2}"
frontend_timeout="${DEERFLOW_FRONTEND_TIMEOUT:-300}"
frontend_allow_unauthenticated="${DEERFLOW_FRONTEND_ALLOW_UNAUTHENTICATED:-1}"

enable_services="${ENABLE_GCP_SERVICES:-1}"
create_repository="${CREATE_ARTIFACT_REGISTRY_REPO:-1}"

ensure_google_services() {
  if [[ "$enable_services" != "1" ]]; then
    return
  fi
  gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    --project "$project_id"
}

ensure_artifact_repository() {
  if gcloud artifacts repositories describe "$repository" \
      --project "$project_id" \
      --location "$region" >/dev/null 2>&1; then
    return
  fi

  [[ "$create_repository" == "1" ]] || die "Artifact Registry repo '$repository' does not exist in $region"
  gcloud artifacts repositories create "$repository" \
    --project "$project_id" \
    --location "$region" \
    --repository-format docker \
    --description "DeerFlow Cloud Run images"
}

build_images() {
  ensure_google_services
  ensure_artifact_repository
  gcloud builds submit "$repo_root" \
    --project "$project_id" \
    --config "$script_dir/cloudbuild.yaml" \
    --substitutions "_BACKEND_IMAGE=${backend_image},_FRONTEND_IMAGE=${frontend_image}"
}

secret_args=()
add_secret() {
  local env_name="$1"
  local secret_name="$2"
  if [[ -n "$secret_name" ]]; then
    secret_args+=("${env_name}=${secret_name}:latest")
  fi
}

env_args=()
add_env() {
  local env_name="$1"
  local value="$2"
  if [[ -n "$value" ]]; then
    env_args+=("${env_name}=${value}")
  fi
}

deploy_backend() {
  secret_args=()
  add_secret DATABASE_URL "${DATABASE_URL_SECRET_NAME:-deerflow-database-url}"
  add_secret VERCEL_TOKEN "${VERCEL_TOKEN_SECRET_NAME:-deerflow-vercel-token}"
  add_secret VERCEL_PROJECT_ID "${VERCEL_PROJECT_ID_SECRET_NAME:-deerflow-vercel-project-id}"
  add_secret VERCEL_TEAM_ID "${VERCEL_TEAM_ID_SECRET_NAME:-}"
  add_secret OPENAI_API_KEY "${OPENAI_API_KEY_SECRET_NAME:-}"
  add_secret BETTER_AUTH_SECRET "${BETTER_AUTH_SECRET_NAME:-deerflow-better-auth-secret}"
  add_secret AUTH_JWT_SECRET "${AUTH_JWT_SECRET_SECRET_NAME:-deerflow-auth-jwt-secret}"
  add_secret DEER_FLOW_INTERNAL_AUTH_TOKEN "${DEER_FLOW_INTERNAL_AUTH_TOKEN_SECRET_NAME:-deerflow-internal-auth-token}"
  add_secret DEERFLOW_EMBED_TOKEN_SECRET "${DEERFLOW_EMBED_TOKEN_SECRET_NAME:-}"
  add_secret ORPHEUS_AGENT_WORKSPACE_CALLBACK_TOKEN "${ORPHEUS_AGENT_WORKSPACE_CALLBACK_TOKEN_SECRET_NAME:-}"

  env_args=()
  add_env DEER_FLOW_PROJECT_ROOT /app
  add_env DEER_FLOW_HOME /tmp/deer-flow
  add_env DEER_FLOW_CONFIG_PATH /app/ops/deerflow/cloud-run/config.prod.yaml
  add_env DEER_FLOW_EXTENSIONS_CONFIG_PATH /app/ops/deerflow/cloud-run/extensions_config.prod.json
  add_env DEER_FLOW_SKILLS_PATH /app/skills
  add_env DEERFLOW_PUBLIC_BASE_URL "${DEERFLOW_PUBLIC_BASE_URL:-${DEERFLOW_FRONTEND_PUBLIC_URL:-}}"
  add_env ORPHEUS_AGENT_WORKSPACE_CALLBACK_URL "${ORPHEUS_AGENT_WORKSPACE_CALLBACK_URL:-}"
  add_env GATEWAY_CORS_ORIGINS "${GATEWAY_CORS_ORIGINS:-${DEERFLOW_FRONTEND_PUBLIC_URL:-}}"

  local args=(
    run
    deploy
    "$backend_service"
    --project "$project_id"
    --region "$region"
    --platform managed
    --image "$backend_image"
    --port 8001
    --cpu "$backend_cpu"
    --memory "$backend_memory"
    --concurrency "$backend_concurrency"
    --timeout "$backend_timeout"
    --min-instances "$backend_min_instances"
    --max-instances "$backend_max_instances"
    --no-cpu-throttling
    --cpu-boost
  )

  if [[ "$backend_allow_unauthenticated" == "1" ]]; then
    args+=(--allow-unauthenticated)
  else
    args+=(--no-allow-unauthenticated)
  fi

  if ((${#env_args[@]})); then
    args+=(--set-env-vars "$(join_by_comma "${env_args[@]}")")
  fi
  if ((${#secret_args[@]})); then
    args+=(--set-secrets "$(join_by_comma "${secret_args[@]}")")
  fi

  gcloud "${args[@]}"
}

deploy_frontend() {
  local backend_url
  backend_url="${DEERFLOW_BACKEND_PUBLIC_URL:-$(gcloud run services describe "$backend_service" --project "$project_id" --region "$region" --format='value(status.url)')}"
  [[ -n "$backend_url" ]] || die "could not resolve backend Cloud Run URL"

  secret_args=()
  add_secret BETTER_AUTH_SECRET "${BETTER_AUTH_SECRET_NAME:-deerflow-better-auth-secret}"

  env_args=()
  add_env DEER_FLOW_INTERNAL_GATEWAY_BASE_URL "$backend_url"
  add_env NEXT_PUBLIC_BACKEND_BASE_URL "${NEXT_PUBLIC_BACKEND_BASE_URL:-}"
  add_env NEXT_PUBLIC_LANGGRAPH_BASE_URL "${NEXT_PUBLIC_LANGGRAPH_BASE_URL:-}"

  local args=(
    run
    deploy
    "$frontend_service"
    --project "$project_id"
    --region "$region"
    --platform managed
    --image "$frontend_image"
    --port 3000
    --cpu "$frontend_cpu"
    --memory "$frontend_memory"
    --concurrency "$frontend_concurrency"
    --timeout "$frontend_timeout"
    --min-instances "$frontend_min_instances"
    --max-instances "$frontend_max_instances"
  )

  if [[ "$frontend_allow_unauthenticated" == "1" ]]; then
    args+=(--allow-unauthenticated)
  else
    args+=(--no-allow-unauthenticated)
  fi

  if ((${#env_args[@]})); then
    args+=(--set-env-vars "$(join_by_comma "${env_args[@]}")")
  fi
  if ((${#secret_args[@]})); then
    args+=(--set-secrets "$(join_by_comma "${secret_args[@]}")")
  fi

  gcloud "${args[@]}"
}

deploy_services() {
  deploy_backend
  deploy_frontend
  echo
  echo "Backend image:  $backend_image"
  echo "Frontend image: $frontend_image"
  echo "Gateway health:"
  echo "  curl \"$(gcloud run services describe "$backend_service" --project "$project_id" --region "$region" --format='value(status.url)')/health\""
}

case "$command" in
  all)
    build_images
    deploy_services
    ;;
  build)
    build_images
    ;;
  deploy)
    deploy_services
    ;;
esac
