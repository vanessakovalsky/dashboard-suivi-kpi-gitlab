#!/usr/bin/env bash
# Prépare GitLab pour la démo : token admin temporaire, runner, puis données de démo.
# Usage : ./scripts/seed-gitlab.sh [podman|docker]
set -euo pipefail

ENGINE="${1:-podman}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck disable=SC1091
[[ -f "${HERE}/../.env" ]] && set -a && . "${HERE}/../.env" && set +a
GITLAB_URL="${SEED_GITLAB_URL:-http://localhost:${GITLAB_HTTP_PORT:-8929}}"

find_container() {
  "${ENGINE}" ps --filter "name=gitlab-kpi[-_]$1" --format '{{.Names}}' | grep -v exporter | head -n1 || true
}

GITLAB_CT="$(find_container gitlab)"
RUNNER_CT="$(find_container gitlab-runner)"
if [[ -z "${GITLAB_CT}" ]]; then
  echo "GitLab introuvable : '${ENGINE} compose up -d gitlab' d'abord." >&2
  exit 1
fi

echo "Création d'un token admin temporaire (1 jour, scopes api + create_runner)…"
ADMIN_TOKEN="$("${ENGINE}" exec "${GITLAB_CT}" gitlab-rails runner '
  t = User.find_by_username("root").personal_access_tokens.create!(
        name: "seed-demo", scopes: [:api, :create_runner], expires_at: 1.day.from_now)
  puts t.token
' | tail -n1)"

if [[ -n "${RUNNER_CT}" ]]; then
  if ! "${ENGINE}" exec "${RUNNER_CT}" grep -q 'url = "http://gitlab"' /etc/gitlab-runner/config.toml 2>/dev/null; then
    echo "Enregistrement du runner de démo (executor shell)…"
    RUNNER_TOKEN="$(curl -fsS -X POST -H "PRIVATE-TOKEN: ${ADMIN_TOKEN}" \
      --data "runner_type=instance_type" --data "run_untagged=true" \
      --data "description=runner-demo" "${GITLAB_URL}/api/v4/user/runners" |
      python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')"
    "${ENGINE}" exec "${RUNNER_CT}" gitlab-runner register --non-interactive \
      --url http://gitlab --clone-url http://gitlab --token "${RUNNER_TOKEN}" \
      --executor shell --name runner-demo
  fi
else
  echo "Pas de runner : les pipelines resteront 'pending'. Lancez '${ENGINE} compose --profile demo up -d'." >&2
fi

GITLAB_URL="${GITLAB_URL}" GITLAB_TOKEN="${ADMIN_TOKEN}" python3 "${HERE}/seed_gitlab.py"
