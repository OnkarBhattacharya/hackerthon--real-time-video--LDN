#!/usr/bin/env bash
# Push main to GitHub using GH_TOKEN or GITHUB_TOKEN (repo scope).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
REMOTE="${GITHUB_REMOTE:-origin}"
BRANCH="${1:-main}"

if [[ -n "$TOKEN" ]]; then
  gh auth status >/dev/null 2>&1 || printf '%s' "$TOKEN" | gh auth login --with-token
fi

if gh auth status >/dev/null 2>&1; then
  git push -u "$REMOTE" "$BRANCH"
  exit 0
fi

if [[ -z "$TOKEN" ]]; then
  echo "No GitHub auth. Either:" >&2
  echo "  1) Complete device login: gh auth login -h github.com -p https --scopes repo --web" >&2
  echo "  2) Export GITHUB_TOKEN (classic PAT, repo scope) and re-run this script" >&2
  exit 1
fi

git push -u "https://x-access-token:${TOKEN}@github.com/ahmedm87/hackerthon--real-time-video--LDN.git" "$BRANCH"
