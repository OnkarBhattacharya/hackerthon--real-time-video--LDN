#!/bin/bash
# Deploy Idea Upscaler only.
# Does not create, change, or lock the other app.
# Does not set, unset, or restore GUEST_UPSTREAM.
# Does not apply a Deployment manifest.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-/config/team-41-k8s.yaml}"
NS="${NS:-team-41}"
APP_HOST="${APP_HOST:-video-lab-team-41.cosmos.vastdata.com}"

for f in main.py apps.json requirements.txt; do
  if [[ ! -f "$ROOT/$f" ]]; then
    echo "missing $ROOT/$f" >&2
    exit 1
  fi
done

TOKEN_FILE=""
BUCKET_FILE=""
WANDB_KEY_FILE=""
WANDB_TEAM_FILE=""
WANDB_PROJECT_FILE=""
cleanup_token() {
  if [[ -n "${TOKEN_FILE}" && -f "${TOKEN_FILE}" ]]; then
    rm -f "${TOKEN_FILE}"
  fi
  if [[ -n "${BUCKET_FILE}" && -f "${BUCKET_FILE}" ]]; then
    rm -f "${BUCKET_FILE}"
  fi
  if [[ -n "${WANDB_KEY_FILE}" && -f "${WANDB_KEY_FILE}" ]]; then
    rm -f "${WANDB_KEY_FILE}"
  fi
  if [[ -n "${WANDB_TEAM_FILE}" && -f "${WANDB_TEAM_FILE}" ]]; then
    rm -f "${WANDB_TEAM_FILE}"
  fi
  if [[ -n "${WANDB_PROJECT_FILE}" && -f "${WANDB_PROJECT_FILE}" ]]; then
    rm -f "${WANDB_PROJECT_FILE}"
  fi
}
trap cleanup_token EXIT

# Cosmos Reason auth stays out of git. The pod reads /code/gpu-bearer.
if [[ -z "${GPU_BEARER_TOKEN:-}" ]]; then
  mapfile -t _cfgs < <(find /config -maxdepth 1 -type f -name '*.config' | sort)
  if [[ ${#_cfgs[@]} -eq 1 ]]; then
    GPU_BEARER_TOKEN="$(python3 -c 'import sys; from pathlib import Path
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("GPU_BEARER_TOKEN="):
        print(line.split("=", 1)[1].strip(), end="")
        break
' "${_cfgs[0]}")"
  fi
fi

# Bucket name stays out of git. The pod reads /code/chunks-bucket to stream an upload.
if [[ -z "${S3_CHUNKS_BUCKET:-}" ]]; then
  mapfile -t _bucket_cfgs < <(find /config -maxdepth 1 -type f -name '*.config' | sort)
  if [[ ${#_bucket_cfgs[@]} -eq 1 ]]; then
    S3_CHUNKS_BUCKET="$(python3 -c 'import sys; from pathlib import Path
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("S3_CHUNKS_BUCKET="):
        print(line.split("=", 1)[1].strip(), end="")
        break
' "${_bucket_cfgs[0]}")"
  fi
fi

CM_ARGS=(--from-file="$ROOT")
if [[ -n "${GPU_BEARER_TOKEN:-}" ]]; then
  TOKEN_FILE="$(mktemp)"
  printf '%s' "$GPU_BEARER_TOKEN" > "$TOKEN_FILE"
  CM_ARGS+=(--from-file="gpu-bearer=${TOKEN_FILE}")
fi
if [[ -n "${S3_CHUNKS_BUCKET:-}" ]]; then
  BUCKET_FILE="$(mktemp)"
  printf '%s' "$S3_CHUNKS_BUCKET" > "$BUCKET_FILE"
  CM_ARGS+=(--from-file="chunks-bucket=${BUCKET_FILE}")
fi
# W&B key stays out of git. The pod reads /code/wandb-key when the env var is unset.
if [[ -n "${WANDB_API_KEY:-}" ]]; then
  WANDB_KEY_FILE="$(mktemp)"
  printf '%s' "$WANDB_API_KEY" > "$WANDB_KEY_FILE"
  CM_ARGS+=(--from-file="wandb-key=${WANDB_KEY_FILE}")
fi
if [[ -n "${WANDB_TEAM:-}" ]]; then
  WANDB_TEAM_FILE="$(mktemp)"
  printf '%s' "$WANDB_TEAM" > "$WANDB_TEAM_FILE"
  CM_ARGS+=(--from-file="wandb-team=${WANDB_TEAM_FILE}")
fi
if [[ -n "${WANDB_PROJECT:-}" ]]; then
  WANDB_PROJECT_FILE="$(mktemp)"
  printf '%s' "$WANDB_PROJECT" > "$WANDB_PROJECT_FILE"
  CM_ARGS+=(--from-file="wandb-project=${WANDB_PROJECT_FILE}")
fi

kubectl -n "$NS" create configmap hackathon-app-code \
  "${CM_ARGS[@]}" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" rollout restart deploy/hackathon-app
kubectl -n "$NS" rollout status deploy/hackathon-app --timeout=240s

for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  code="$(curl -sS -m 8 -o /dev/null -w '%{http_code}' "http://${APP_HOST}/app/upscaler" || true)"
  if [[ "$code" == "200" ]]; then
    echo "upscaler 200"
    exit 0
  fi
  sleep 5
done

echo "upscaler did not return 200" >&2
exit 1
