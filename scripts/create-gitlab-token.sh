#!/usr/bin/env bash
# Crée un utilisateur de service "kpi-bot" et un token read_api (lecture seule de l'API).
# Usage : ./scripts/create-gitlab-token.sh [podman|docker]
set -euo pipefail

ENGINE="${1:-podman}"
CONTAINER="$("${ENGINE}" ps --filter "name=gitlab-kpi[-_]gitlab" --format '{{.Names}}' | grep -v exporter | head -n1 || true)"
if [[ -z "${CONTAINER}" ]]; then
  echo "Conteneur GitLab introuvable : lancez d'abord '${ENGINE} compose up -d gitlab'." >&2
  exit 1
fi

# kpi-bot est admin pour voir tous les projets du groupe ; le token reste limité à read_api.
TOKEN="$("${ENGINE}" exec "${CONTAINER}" gitlab-rails runner '
  user = User.find_by_username("kpi-bot") || begin
    u = User.new(username: "kpi-bot", name: "KPI bot", email: "kpi-bot@example.invalid",
                 password: SecureRandom.hex(24), skip_confirmation: true, admin: true)
    u.assign_personal_namespace(Organizations::Organization.default_organization) if u.respond_to?(:assign_personal_namespace)
    u.save!
    u
  end
  t = user.personal_access_tokens.create!(name: "prometheus-kpi", scopes: [:read_api],
                                         expires_at: 365.days.from_now)
  puts t.token
' | tail -n1)"

echo "Token créé. Ajoutez-le dans .env :"
echo "GITLAB_TOKEN=${TOKEN}"
