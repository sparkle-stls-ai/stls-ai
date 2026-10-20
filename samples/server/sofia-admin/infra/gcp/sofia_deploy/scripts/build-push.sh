#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# build-push.sh — Build & push delle 3 immagini Sofia su Artifact Registry
#
# Struttura attesa (build context = docker/server-{nome}/):
#   sofia_deploy/
#   └── docker/
#       ├── server-api-keys/
#       │   ├── Dockerfile
#       │   ├── requirements.txt
#       │   ├── wheels/stls_a2a-*.whl
#       │   └── app/
#       ├── server-monitoring/  (idem)
#       └── server-redis/       (idem)
# ---------------------------------------------------------------------------
set -euo pipefail

TAG="${1:-latest}"
PROJECT_ID="${2:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${3:-europe-west8}"

REGISTRY="${REGION}-docker.pkg.dev/${PROJECT_ID}/sofia"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(dirname "$SCRIPT_DIR")"
DOCKER_DIR="${DEPLOY_DIR}/docker"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
log()  { echo -e "${GREEN}[build-push]${NC} $*"; }
warn() { echo -e "${YELLOW}[build-push]${NC} $*"; }
err()  { echo -e "${RED}[build-push]${NC} $*" >&2; }

for cmd in gcloud docker; do
  command -v "$cmd" &>/dev/null || { err "Comando '$cmd' non trovato."; exit 1; }
done

log "Progetto  : ${PROJECT_ID}"
log "Regione   : ${REGION}"
log "Registry  : ${REGISTRY}"
log "Tag       : ${TAG}"
log "Docker dir: ${DOCKER_DIR}"
echo

gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

BUILD_ERRORS=()

for agent in api-keys monitoring redis; do
  # Build context e Dockerfile sono nella stessa directory
  SERVER_DIR="${DOCKER_DIR}/server-${agent}"
  DOCKERFILE="${SERVER_DIR}/Dockerfile"
  IMAGE="${REGISTRY}/server-${agent}:${TAG}"

  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  log "Building server-${agent}"
  log "  Context    : ${SERVER_DIR}"
  log "  Dockerfile : ${DOCKERFILE}"
  log "  Image      : ${IMAGE}"

  if [[ ! -d "$SERVER_DIR" ]]; then
    warn "Directory non trovata: ${SERVER_DIR}"
    BUILD_ERRORS+=("$agent: directory non trovata"); continue
  fi
  if [[ ! -f "$DOCKERFILE" ]]; then
    warn "Dockerfile non trovato: ${DOCKERFILE}"
    BUILD_ERRORS+=("$agent: Dockerfile non trovato"); continue
  fi
  if [[ ! -d "${SERVER_DIR}/app" ]]; then
    warn "Cartella app/ mancante in: ${SERVER_DIR}"
    BUILD_ERRORS+=("$agent: app/ non trovata"); continue
  fi
  if [[ -z "$(ls "${SERVER_DIR}/wheels"/stls_a2a-*.whl 2>/dev/null)" ]]; then
    warn "Nessun wheel stls_a2a-*.whl in: ${SERVER_DIR}/wheels/"
    BUILD_ERRORS+=("$agent: wheel non trovato"); continue
  fi

  if docker build \
      -f "$DOCKERFILE" \
      -t "$IMAGE" \
      --build-arg BUILDKIT_INLINE_CACHE=1 \
      --label "build.date=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      "$SERVER_DIR"; then
    log "Push ${IMAGE}…"
    docker push "$IMAGE"
    log "✓ server-${agent} completato"
  else
    err "✗ Build fallita per server-${agent}"
    BUILD_ERRORS+=("$agent: build fallita")
  fi
  echo
done

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
if [[ ${#BUILD_ERRORS[@]} -eq 0 ]]; then
  log "✓ Tutti i servizi buildati e pushati con tag '${TAG}'"
  echo
  log "Prossimo passo:"
  echo "  cd ${DEPLOY_DIR}/terraform && terraform apply -var='default_image_tag=${TAG}'"
else
  err "Errori:"
  for e in "${BUILD_ERRORS[@]}"; do err "  - $e"; done
  exit 1
fi