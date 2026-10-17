#!/bin/bash
# Update Idea Upscaler code only. Do not replace the Deployment.
# That keeps GUEST_UPSTREAM and the other app's Service intact.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-/config/team-41-k8s.yaml}"
NS="${NS:-team-41}"
APP_HOST="${APP_HOST:-video-lab-team-41.cosmos.vastdata.com}"

need=(main.py video_ask_app.py vss_client.py index.html app.js style.css apps.json requirements.txt)
for f in "${need[@]}"; do
  if [[ ! -f "$ROOT/$f" ]]; then
    echo "refusing deploy: missing $f (would drop the other app from the bundle)" >&2
    exit 1
  fi
done

GUEST_BEFORE="$(kubectl -n "$NS" get deploy hackathon-app -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="GUEST_UPSTREAM")]}{.value}{end}')"
if [[ -z "$GUEST_BEFORE" ]]; then
  echo "refusing deploy: GUEST_UPSTREAM is empty, the other app is not wired" >&2
  exit 1
fi

kubectl -n "$NS" create configmap hackathon-app-code \
  --from-file="$ROOT" \
  --dry-run=client -o yaml | kubectl apply -f -

# Code only. Do not kubectl apply a Deployment manifest.
kubectl -n "$NS" rollout restart deploy/hackathon-app
kubectl -n "$NS" rollout status deploy/hackathon-app --timeout=240s

GUEST_AFTER="$(kubectl -n "$NS" get deploy hackathon-app -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="GUEST_UPSTREAM")]}{.value}{end}')"
if [[ "$GUEST_AFTER" != "$GUEST_BEFORE" ]]; then
  echo "GUEST_UPSTREAM changed during restart; putting the other app back" >&2
  kubectl -n "$NS" set env deploy/hackathon-app "GUEST_UPSTREAM=$GUEST_BEFORE"
  exit 1
fi

for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  code="$(curl -sS -m 8 -o /dev/null -w '%{http_code}' "http://${APP_HOST}/app/health" || true)"
  [[ "$code" == "200" ]] && break
  sleep 5
done

upscaler="$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "http://${APP_HOST}/app/upscaler" || true)"
guest="$(curl -sS -m 20 -o /dev/null -w '%{http_code}' "http://${APP_HOST}/app/guest" || true)"
echo "upscaler $upscaler"
echo "guest $guest"
[[ "$upscaler" == "200" && "$guest" == "200" ]]
