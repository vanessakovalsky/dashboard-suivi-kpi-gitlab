"""Injecte des données de démo dans GitLab pour alimenter le dashboard.

Crée (ou réutilise) : 4 développeurs, un groupe, 3 projets avec une CI qui échoue
aléatoirement, des labels workflow::, 3 jalons, des issues pondérées (créées dans le
passé, une partie fermées), des MR (brouillons, ouvertes, mergées) et des pipelines.

Usage (via scripts/seed-gitlab.sh, qui fournit un token admin `api`) :
    GITLAB_URL=http://localhost:8929 GITLAB_TOKEN=... python3 scripts/seed_gitlab.py
Options : SEED_GROUP (equipe-demo), SEED_ISSUES_PER_PROJECT (25), SEED_PIPELINES (8),
          SEED_RANDOM (42, pour des données reproductibles).
"""

from __future__ import annotations

import json
import os
import random
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from typing import Any

USERS = [
    ("alice", "Alice Martin"),
    ("bob", "Bob Durand"),
    ("chloe", "Chloé Bernard"),
    ("david", "David Petit"),
]
PROJECTS = {"api": 20, "web": 35, "mobile": 10}  # projet -> % d'échec du job de test
WORKFLOW = ["todo", "doing", "review"]
LABELS = {
    "workflow::todo": "#6699cc",
    "workflow::doing": "#f0ad4e",
    "workflow::review": "#9b59b6",
    "type::bug": "#d9534f",
    "type::feature": "#5cb85c",
}
TITLES = [
    "Ajouter la pagination",
    "Corriger le timeout",
    "Refonte du formulaire",
    "Exporter en CSV",
    "Cache des sessions",
    "Logs structurés",
    "Mode sombre",
    "Notifications e-mail",
    "Recherche plein texte",
    "Migration de schéma",
    "Tests de charge",
    "Accessibilité du menu",
    "Quota par utilisateur",
    "Webhooks sortants",
    "Audit des droits",
    "Import en masse",
]

CI_YAML = """stages: [build, test, deploy]

build:
  stage: build
  script:
    - echo "build $CI_PROJECT_NAME"
    - sleep 2

test:
  stage: test
  script:
    - echo "Taux d'échec simulé ${FAIL_RATE}%"
    - awk -v r="${FAIL_RATE:-20}" 'BEGIN{srand(); exit (rand()*100 < r)}'

deploy:
  stage: deploy
  script: [echo "deploy"]
  rules:
    - if: $CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH
"""


class Api:
    def __init__(self, url: str, token: str) -> None:
        if not url.startswith(("http://", "https://")):
            raise SystemExit("GITLAB_URL doit commencer par http:// ou https://")
        self.base = url.rstrip("/") + "/api/v4"
        self.token = token

    def call(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        ok_missing: bool = False,
    ) -> Any:
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=body,
            method=method,
            headers={"PRIVATE-TOKEN": self.token, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            if ok_missing and exc.code == 404:
                return None
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"{method} {path} -> {exc.code} {detail}") from exc

    def get(self, path: str, /, **params: Any) -> Any:
        query = "?" + urllib.parse.urlencode(params) if params else ""
        return self.call("GET", path + query, ok_missing=True)

    def post(self, path: str, /, **data: Any) -> Any:
        return self.call("POST", path, data)

    def put(self, path: str, /, **data: Any) -> Any:
        return self.call("PUT", path, data)


def q(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def ensure_user(api: Api, username: str, name: str) -> int:
    found = api.get("/users", username=username)
    if found:
        return int(found[0]["id"])
    user = api.post(
        "/users",
        username=username,
        name=name,
        email=f"{username}@example.invalid",
        skip_confirmation=True,
        password=secrets.token_urlsafe(18) + "aA1!",
    )
    return int(user["id"])


def ensure_group(api: Api, path: str) -> int:
    group = api.get(f"/groups/{q(path)}")
    if group:
        return int(group["id"])
    return int(api.post("/groups", name=path, path=path, visibility="private")["id"])


def ensure_project(
    api: Api, group_id: int, group_path: str, name: str, fail_rate: int
) -> int:
    proj = api.get(f"/projects/{q(group_path + '/' + name)}")
    if proj is None:
        proj = api.post(
            "/projects",
            name=name,
            path=name,
            namespace_id=group_id,
            initialize_with_readme=True,
            default_branch="main",
        )
        time.sleep(2)
        api.post(
            f"/projects/{proj['id']}/repository/commits",
            branch="main",
            commit_message="CI de démonstration",
            actions=[
                {"action": "create", "file_path": ".gitlab-ci.yml", "content": CI_YAML}
            ],
        )
        api.post(
            f"/projects/{proj['id']}/variables", key="FAIL_RATE", value=str(fail_rate)
        )
    return int(proj["id"])


def ensure_labels_and_milestones(api: Api, group_id: int, today: date) -> list[int]:
    existing = {
        lbl["name"] for lbl in api.get(f"/groups/{group_id}/labels", per_page=100) or []
    }
    for name, color in LABELS.items():
        if name not in existing:
            api.post(f"/groups/{group_id}/labels", name=name, color=color)

    milestones = {
        m["title"]: int(m["id"])
        for m in api.get(f"/groups/{group_id}/milestones", per_page=100) or []
    }
    monday = today - timedelta(days=today.weekday())
    ids = []
    for offset, label in ((-14, "précédent"), (0, "en cours"), (14, "suivant")):
        start = monday + timedelta(days=offset)
        title = f"Sprint {start.isocalendar().week:02d} ({label})"
        if title not in milestones:
            m = api.post(
                f"/groups/{group_id}/milestones",
                title=title,
                start_date=start.isoformat(),
                due_date=(start + timedelta(days=13)).isoformat(),
            )
            milestones[title] = int(m["id"])
        ids.append(milestones[title])
    return ids  # [précédent, en cours, suivant]


def seed_issues(
    api: Api,
    rng: random.Random,
    pid: int,
    user_ids: list[int],
    milestone_ids: list[int],
    count: int,
    now: datetime,
) -> None:
    prev, cur, nxt = milestone_ids
    for _ in range(count):
        created = now - timedelta(days=rng.randint(0, 30), hours=rng.randint(0, 23))
        milestone = rng.choices([prev, cur, nxt, None], weights=[3, 5, 2, 1])[0]
        closed = (milestone == prev and rng.random() < 0.85) or rng.random() < 0.25
        weight = rng.choice([1, 1, 2, 3, 3, 5, 8])
        # GitLab CE ignore "weight" (Premium) : le label weight::N sert de repli à l'exporter
        labels = [rng.choice(["type::bug", "type::feature"]), f"weight::{weight}"]
        if not closed:
            labels.append("workflow::" + rng.choices(WORKFLOW, weights=[4, 3, 2])[0])
        data: dict[str, Any] = {
            "title": rng.choice(TITLES),
            "labels": ",".join(labels),
            "weight": weight,
            "created_at": created.isoformat(),
        }
        if milestone:
            data["milestone_id"] = milestone
        if rng.random() < 0.85:
            data["assignee_ids"] = [rng.choice(user_ids)]
        if rng.random() < 0.4:
            data["due_date"] = (
                now.date() + timedelta(days=rng.randint(-7, 14))
            ).isoformat()
        issue = api.post(f"/projects/{pid}/issues", **data)
        if closed:
            api.put(f"/projects/{pid}/issues/{issue['iid']}", state_event="close")


def seed_merge_requests(
    api: Api, rng: random.Random, pid: int, user_ids: list[int]
) -> None:
    for i in range(rng.randint(4, 7)):
        branch = f"feature/{rng.randint(100, 999)}-{i}"
        api.post(
            f"/projects/{pid}/repository/commits",
            branch=branch,
            start_branch="main",
            commit_message=f"Travail sur {branch}",
            actions=[
                {
                    "action": "create",
                    "file_path": f"src/{branch}.txt",
                    "content": "démo\n",
                }
            ],
        )
        draft = rng.random() < 0.3
        mr = api.post(
            f"/projects/{pid}/merge_requests",
            source_branch=branch,
            target_branch="main",
            assignee_id=rng.choice(user_ids),
            title=("Draft: " if draft else "") + rng.choice(TITLES),
            remove_source_branch=True,
        )
        if not draft and rng.random() < 0.5:
            for _ in range(10):  # le calcul de mergeabilité est asynchrone
                try:
                    api.put(f"/projects/{pid}/merge_requests/{mr['iid']}/merge")
                    break
                except RuntimeError:
                    time.sleep(3)


def trigger_pipelines(api: Api, pid: int, count: int) -> None:
    for _ in range(count):
        api.post(f"/projects/{pid}/pipeline", ref="main")


def main() -> None:
    api = Api(
        os.environ.get("GITLAB_URL", "http://localhost:8929"),
        os.environ.get("GITLAB_TOKEN") or sys.exit("GITLAB_TOKEN manquant"),
    )
    group_path = os.getenv("SEED_GROUP", "equipe-demo")
    per_project = int(os.getenv("SEED_ISSUES_PER_PROJECT", "25"))
    pipelines = int(os.getenv("SEED_PIPELINES", "8"))
    # Aléatoire non cryptographique volontaire : données de démo reproductibles.
    rng = random.Random(int(os.getenv("SEED_RANDOM", "42")))  # nosec B311
    now = datetime.now(UTC)

    print("Utilisateurs…")
    user_ids = [ensure_user(api, u, n) for u, n in USERS]
    print(f"Groupe {group_path}…")
    group_id = ensure_group(api, group_path)
    for uid in user_ids:
        try:
            api.post(f"/groups/{group_id}/members", user_id=uid, access_level=30)
        except RuntimeError:
            pass  # déjà membre
    milestone_ids = ensure_labels_and_milestones(api, group_id, now.date())

    for name, fail_rate in PROJECTS.items():
        print(f"Projet {name} (échec simulé {fail_rate} %)…")
        pid = ensure_project(api, group_id, group_path, name, fail_rate)
        seed_issues(api, rng, pid, user_ids, milestone_ids, per_project, now)
        seed_merge_requests(api, rng, pid, user_ids)
        trigger_pipelines(api, pid, pipelines)

    print(
        f"\nTerminé. Mettez GITLAB_GROUP={group_path} dans .env puis relancez les exporters."
    )


if __name__ == "__main__":
    main()
