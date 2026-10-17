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

kubectl -n "$NS" create configmap hackathon-app-code \
  --from-file="$ROOT" \
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
