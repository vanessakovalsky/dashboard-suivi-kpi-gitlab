# Dashboard de suivi KPI GitLab

Stack **GitLab CE + Prometheus + Grafana** (Podman ou Docker Compose) avec un dashboard
« GitLab — Activité de l'équipe » : état des work items, reste à faire, MR, pipelines,
taux d'échec et santé de l'instance.

## 1. Présentation et architecture

Approche retenue : **hybride**.

```
             ┌───────────────────────────── GitLab CE ─────────────────────────────┐
             │  API REST v4          exporters natifs (Rails, Puma, Sidekiq, ...)  │
             └────┬──────────────┬───────────────────────┬─────────────────────────┘
                  │              │                       │
  workitems-exporter (maison)   gitlab-ci-pipelines-exporter   │ scrape direct
  issues, poids, jalons, MR,    statut/durée des pipelines     │
  lead time, taux d'échec 7 j   et des jobs par projet/ref     │
                  └──────────────┴───────────┬───────────┘
                                         Prometheus  ──►  Grafana (dashboard provisionné)
```

| Source | Ce qu'elle fournit |
|---|---|
| `exporter/` (Python, maison) | issues ouvertes / par étape `workflow::` / par jalon / par personne, sans assigné, en retard, **reste à faire** (somme des poids), créées et fermées sur 7 j, lead time, MR ouvertes / brouillons / âge / temps de merge, **taux d'échec des pipelines sur 7 j** |
| `gitlab-ci-pipelines-exporter` | dernier statut de pipeline par projet et ref, durée, jobs en échec, nombre d'exécutions |
| Exporters natifs GitLab | santé de l'instance (cibles up, requêtes HTTP, Sidekiq) |

### Contenu du dashboard
- **Vue d'ensemble** : issues ouvertes, reste à faire, retards, sans assigné, MR ouvertes, taux d'échec, débit 7 j, lead time, temps de merge, état de la collecte.
- **Work items** : répartition par étape de workflow, reste à faire par jalon, charge par personne, burndown, débit créées vs fermées.
- **Merge requests** : ouvertes et brouillons, plus vieille MR, temps moyen de merge.
- **Pipelines CI/CD** : taux d'échec par projet, statuts sur 7 j, durées, dernier pipeline par ref, jobs en échec, rythme d'exécution.
- **Santé de l'instance**.

Filtres : `Projet` et `Jalon`.

**Conventions côté équipe** pour des données utiles :
- labels scopés `workflow::todo`, `workflow::doing`, `workflow::review`… (préfixe configurable via `WORKFLOW_LABEL_PREFIX`) ;
- poids sur les issues ;
- jalons et dates d'échéance.

Le dashboard est généré par `scripts/build_dashboard.py`. Modifiez ce script puis régénérez le JSON ; la CI vérifie que les deux restent synchronisés.

## 2. Prérequis
- Podman ≥ 4.9 avec `podman compose` (ou `podman-compose`), ou Docker ≥ 24 avec Compose v2.
- **8 Go de RAM conseillés** (4 Go minimum pour GitLab seul).
- Ports libres : 8929 (GitLab), 2224 (SSH GitLab), 9090 (Prometheus), 3000 (Grafana).
- Sans conteneur : Python 3.12+ pour l'exporter.

## 3. Installation sans conteneur (exporter seul, GitLab existant)

Utile pour brancher l'exporter sur un GitLab et un Prometheus déjà en place.

```bash
cd exporter
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export GITLAB_URL=https://gitlab.example.com GITLAB_GROUP=mon-equipe
export GITLAB_TOKEN=glpat-xxxx            # token read_api
python -m workitems_exporter               # expose :9200/metrics
```

Ensuite :
- ajoutez la cible `hote:9200` à votre Prometheus (voir `prometheus/prometheus.yml`) ;
- installez [gitlab-ci-pipelines-exporter](https://github.com/mvisonneau/gitlab-ci-pipelines-exporter) avec `gcpe/gitlab-ci-pipelines-exporter.yml` ;
- importez `grafana/dashboards/gitlab-team-activity.json` dans Grafana (datasource Prometheus d'UID `prometheus`).

## 4. Installation avec conteneurs

```bash
cp .env.example .env        # changez les mots de passe
podman compose up -d gitlab prometheus grafana
# Attendre que GitLab soit "healthy" (5 à 10 min au premier démarrage)
podman compose ps
```

1. Connectez-vous à <http://localhost:8929> avec `root` et le mot de passe `GITLAB_ROOT_PASSWORD`. Créez le groupe de l'équipe (ou importez des projets).
2. Créez le token de lecture pour les exporters :
   ```bash
   ./scripts/create-gitlab-token.sh podman     # ou docker
   ```
3. Dans `.env`, renseignez `GITLAB_TOKEN=...` et `GITLAB_GROUP=<chemin du groupe>`.
4. Démarrez les exporters :
   ```bash
   podman compose --profile exporters up -d --build
   ```
5. Ouvrez le dashboard sur <http://localhost:3000>. Il est configuré comme page d'accueil de Grafana.

### Données de démo (pour présenter le dashboard)

```bash
podman compose --profile demo up -d          # runner shell, pour exécuter les pipelines
./scripts/seed-gitlab.sh podman              # ou docker
# Dans .env : GITLAB_GROUP=equipe-demo, puis :
podman compose --profile exporters up -d --build
```

Le script commence par créer un token admin temporaire (1 jour) et enregistrer le runner. Il crée ensuite :
- 4 développeurs, dans un groupe `equipe-demo` ;
- 3 projets (`api`, `web`, `mobile`) ;
- une CI dont le job `test` échoue à 20 %, 35 % et 10 % selon le projet ;
- les labels `workflow::` et 3 sprints (précédent, en cours, suivant) ;
- environ 75 issues pondérées, antidatées sur 30 jours et en partie fermées ;
- des MR en brouillon, ouvertes et mergées ;
- des pipelines.

Variables utiles :
- `SEED_ISSUES_PER_PROJECT` (25 par défaut) et `SEED_PIPELINES` (8 par défaut) règlent le volume.
- `SEED_RANDOM` fixe la graine aléatoire, pour des données reproductibles.

Le script est **additif** : chaque exécution ajoute des issues, des MR et des pipelines. Relancez-le pour faire bouger les courbes.

Les courbes (burndown, débit) se construisent à partir du démarrage de Prometheus. Pour une démo, laissez tourner la stack quelques heures, ou relancez le seed plusieurs fois avec un intervalle.

Avec Docker, remplacez `podman compose` par `docker compose`.

Les montages utilisent `:Z` pour SELinux (Fedora/RHEL), ce qui est sans effet ailleurs.

### Données simulées sans GitLab (Prometheus + Grafana existants, airgap)

Quand il est impossible de lancer GitLab ou de brancher l'exporter, utilisez `scripts/simulate_gitlab_metrics.py`. Il ne demande que **Python 3.12+ et sa bibliothèque standard**, sans `pip install`.

Il produit **toutes** les séries du dashboard sous les noms et labels des vrais exporters :
- 3 projets et des sprints de 14 jours, avec burndown et ajout de périmètre ;
- le workflow, les retards en fin de sprint, les MR ;
- des pipelines avec des taux d'échec différents par projet et des incidents ponctuels ;
- le trafic de l'instance GitLab.

Les valeurs dépendent uniquement de l'heure : l'historique importé et le flux en direct se raccordent sans rupture.

**1. Remplir 30 jours d'historique** (facultatif, mais sans cela les courbes démarrent à vide) :

```bash
python3 scripts/simulate_gitlab_metrics.py backfill --days 30 --out kpi.om   # ~300 Mo, 15 s
promtool tsdb create-blocks-from openmetrics --max-block-duration=24h kpi.om ./blocks   # ~1 min
```

Copiez ensuite le contenu de `./blocks/` dans le dossier de données de Prometheus, puis redémarrez Prometheus.

Avec Prometheus en conteneur, en une seule commande :

```bash
podman run --rm --user 65534 -v ./kpi.om:/kpi.om:ro,Z -v <volume-data-prometheus>:/prometheus \
  --entrypoint promtool docker.io/prom/prometheus:v3.5.0 \
  tsdb create-blocks-from openmetrics --max-block-duration=24h /kpi.om /prometheus
podman restart <conteneur-prometheus>
```

- La rétention de Prometheus (`--storage.tsdb.retention.time`) doit dépasser `--days`, sinon les blocs importés sont supprimés.
- Gardez `--step` à 5 minutes au maximum (valeur par défaut). Au-delà, Grafana affiche des trous à cause de la fenêtre de 5 minutes de Prometheus.

**2. Alimenter en continu** :

```bash
python3 scripts/simulate_gitlab_metrics.py serve --port 9200
```

Ajoutez ensuite le bloc `prometheus/simulator-scrape.yml` à votre Prometheus, en remplaçant `HOTE_DU_SIMULATEUR`, et rechargez la configuration. **`honor_labels: true` et le relabel `instance` sont nécessaires** pour que les séries correspondent à l'historique.

**3. Grafana** : importez `grafana/dashboards/gitlab-team-activity.json`. Il attend une datasource Prometheus d'UID `prometheus`. Si la vôtre a un autre UID, remplacez-le dans le JSON avant l'import.

## 5. Tests, qualité, sécurité (local)

```bash
cd exporter && pip install -e ".[dev]"
ruff check . && ruff format --check . && mypy workitems_exporter
pytest                                   # unitaires + fonctionnels (faux GitLab HTTP)
bandit -q -r workitems_exporter && pip-audit -r requirements.txt
cd ..
hadolint exporter/Containerfile
shellcheck scripts/*.sh
promtool check config prometheus/prometheus.yml
gitleaks git -v .
podman build -t workitems-exporter -f exporter/Containerfile exporter && trivy image workitems-exporter
```

## 6. Déploiement sans conteneur

Service systemd pour l'exporter, avec Prometheus et Grafana installés via les paquets de la distribution :

```ini
# /etc/systemd/system/gitlab-workitems-exporter.service
[Unit]
Description=GitLab work items exporter
After=network-online.target
[Service]
User=exporter
EnvironmentFile=/etc/gitlab-workitems-exporter.env   # GITLAB_URL, GITLAB_TOKEN, GITLAB_GROUP (chmod 600)
ExecStart=/opt/workitems-exporter/.venv/bin/python -m workitems_exporter
Restart=on-failure
NoNewPrivileges=true
ProtectSystem=strict
[Install]
WantedBy=multi-user.target
```

Copiez ensuite :
- `prometheus/*.yml` dans `/etc/prometheus/` ;
- `grafana/provisioning/*` dans `/etc/grafana/provisioning/`, en ajustant `options.path` vers le dossier du JSON.

## 7. Déploiement avec conteneurs

```bash
podman build -t registry.example.com/kpi/workitems-exporter:0.1.0 -f exporter/Containerfile exporter
podman push registry.example.com/kpi/workitems-exporter:0.1.0
```

Remplacez ensuite `build:` par `image:` dans `compose.yaml`, puis lancez `podman compose --profile exporters up -d`.

En production, pointez `GITLAB_URL` vers votre GitLab et retirez le service `gitlab` de la stack.

## 8. CI

Les jobs sont identiques sur GitHub (`.github/workflows/ci.yml`) et GitLab (`.gitlab-ci.yml`), et tous sont bloquants.

| Étape | Contenu |
|---|---|
| lint | ruff, mypy, dashboard JSON à jour, hadolint, shellcheck, promtool, `compose config` (GitHub) |
| security | bandit, semgrep (python + secrets), pip-audit, gitleaks (historique complet) |
| test | pytest + couverture ≥ 80 % (rapport Cobertura sur GitLab) |
| build | image de l'exporter |
| scan | Trivy HIGH/CRITICAL (`--ignore-unfixed`) |

**Variables à configurer** :
- GitHub : aucune, `GITHUB_TOKEN` est fourni automatiquement pour gitleaks.
- GitLab : aucune pour la CI. Un runner avec Docker-in-Docker (`privileged`) est nécessaire pour le job `build`.

## 9. Sécurité

Résultats au moment de la livraison :
- bandit : 0 alerte.
- pip-audit : aucune vulnérabilité connue.
- hadolint et shellcheck : 0 alerte.

Choix de sécurité :
- Le token GitLab est en **`read_api` uniquement**, expire au bout d'un an et n'est passé que par `.env` (ignoré par git).
- Le compte `kpi-bot` est administrateur pour voir tous les projets du groupe ; le scope du token l'empêche toute écriture.
- Alternative possible : un token de groupe `read_api` avec le rôle Reporter.
- Les exporters natifs de GitLab ne sont **pas publiés** sur l'hôte. Ils restent joignables seulement depuis les réseaux privés (`monitoring_whitelist`).
- L'image de l'exporter tourne en utilisateur non-root (uid 10001), en multi-stage, sans dépendance hors `prometheus-client`.
- L'exporter n'accepte que des URL `http(s)://`, ce qui justifie les `# noqa: S310` / `# nosec B310`.
- `gitlab-ci-pipelines-exporter` est utilisé avec `enable_tls_verify: false` uniquement parce que GitLab est joint en HTTP interne dans la stack. **Réactivez la vérification TLS en production.**
- Le mot de passe root GitLab et le mot de passe admin Grafana sont **obligatoires** dans `.env` : compose refuse de démarrer sinon.

Limites connues :
- Le « reste à faire » repose sur les poids des issues : sans poids, utilisez le nombre d'issues ouvertes par jalon.
- Les tags d'images sont épinglés dans `.env.example` ; vérifiez qu'ils correspondent à la dernière version stable avant une mise en production.
- Le maintien actif de `gitlab-ci-pipelines-exporter` est à surveiller. En cas d'abandon, l'exporter maison couvre déjà le taux d'échec et les statuts sur 7 j.
