"""Calcul pur des métriques d'activité d'équipe à partir des données GitLab.

Aucune entrée/sortie ici : les fonctions prennent des listes de dictionnaires
(tels que renvoyés par l'API REST GitLab) et renvoient des échantillons. Cela
rend toute la logique métier testable sans GitLab.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

Labels = tuple[tuple[str, str], ...]

FAILED_STATUSES = {"failed"}
FINISHED_STATUSES = {"success", "failed", "canceled"}


@dataclass
class Snapshot:
    """Ensemble des séries calculées : nom de métrique -> {labels: valeur}."""

    gauges: dict[str, dict[Labels, float]] = field(default_factory=lambda: defaultdict(dict))

    def set(self, name: str, value: float, **labels: str) -> None:
        self.gauges[name][tuple(sorted(labels.items()))] = float(value)

    def add(self, name: str, value: float, **labels: str) -> None:
        key = tuple(sorted(labels.items()))
        self.gauges[name][key] = self.gauges[name].get(key, 0.0) + float(value)

    def get(self, name: str, **labels: str) -> float | None:
        return self.gauges.get(name, {}).get(tuple(sorted(labels.items())))


def parse_ts(value: str | None) -> datetime | None:
    """Parse un horodatage ISO 8601 GitLab (avec 'Z' ou décalage)."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _project_of(item: dict[str, Any], projects: dict[int, str]) -> str:
    pid = item.get("project_id")
    if isinstance(pid, int) and pid in projects:
        return projects[pid]
    refs = item.get("references") or {}
    full = refs.get("full", "")
    # "groupe/projet#12" ou "groupe/projet!3"
    for sep in ("#", "!"):
        if sep in full:
            return str(full.split(sep)[0])
    return "unknown"


def _workflow_label(labels: list[str], workflow_prefix: str) -> str:
    """Retourne l'étape de workflow (ex. label scopé 'workflow::doing')."""
    for label in labels:
        if label.startswith(workflow_prefix):
            return label[len(workflow_prefix) :]
    return "none"


WEIGHT_LABEL_PREFIX = "weight::"


def issue_weight(issue: dict[str, Any], labels: list[str]) -> float:
    """Poids natif (GitLab Premium+), sinon label scopé 'weight::N' (GitLab CE), sinon 0.

    GitLab CE ignore le champ 'weight' : l'API renvoie toujours null.
    """
    native = issue.get("weight")
    if native is not None:
        return float(native)
    for label in labels:
        if label.startswith(WEIGHT_LABEL_PREFIX):
            try:
                return float(label[len(WEIGHT_LABEL_PREFIX) :])
            except ValueError:
                continue
    return 0.0


def compute_issue_metrics(
    snap: Snapshot,
    issues: list[dict[str, Any]],
    projects: dict[int, str],
    now: datetime,
    workflow_prefix: str = "workflow::",
) -> None:
    """Work items : états, workflow, reste à faire, retards, débit, lead time."""
    week_ago = now - timedelta(days=7)
    lead_times: dict[str, list[float]] = defaultdict(list)

    for issue in issues:
        project = _project_of(issue, projects)
        state = str(issue.get("state", "unknown"))
        milestone = (issue.get("milestone") or {}).get("title") or "none"
        assignees = issue.get("assignees") or []
        labels = [str(lbl) for lbl in issue.get("labels") or []]
        weight = issue_weight(issue, labels)

        if state == "opened":
            snap.add("gitlab_issues_open", 1, project=project)
            snap.add(
                "gitlab_issues_open_by_workflow",
                1,
                project=project,
                workflow=_workflow_label(labels, workflow_prefix),
            )
            snap.add("gitlab_issues_weight_remaining", weight, project=project, milestone=milestone)
            snap.add("gitlab_issues_open_by_milestone", 1, project=project, milestone=milestone)
            if not assignees:
                snap.add("gitlab_issues_unassigned", 1, project=project)
            for assignee in assignees:
                snap.add(
                    "gitlab_issues_open_by_assignee",
                    1,
                    assignee=str(assignee.get("username", "unknown")),
                )
            due = issue.get("due_date")
            if due and datetime.fromisoformat(due).date() < now.date():
                snap.add("gitlab_issues_overdue", 1, project=project)
            # Garantit l'existence des séries (0 plutôt qu'absence)
            snap.add("gitlab_issues_overdue", 0, project=project)
            snap.add("gitlab_issues_unassigned", 0, project=project)
        elif state == "closed":
            snap.add("gitlab_issues_weight_done", weight, project=project, milestone=milestone)
            closed_at = parse_ts(issue.get("closed_at"))
            created_at = parse_ts(issue.get("created_at"))
            if closed_at and closed_at >= week_ago:
                snap.add("gitlab_issues_closed_7d", 1, project=project)
            if closed_at and created_at:
                lead_times[project].append((closed_at - created_at).total_seconds())

        snap.add("gitlab_issues_opened_7d", 0, project=project)
        snap.add("gitlab_issues_closed_7d", 0, project=project)
        created_at = parse_ts(issue.get("created_at"))
        if created_at and created_at >= week_ago:
            snap.add("gitlab_issues_opened_7d", 1, project=project)

    for project, values in lead_times.items():
        snap.set("gitlab_issues_lead_time_avg_seconds", sum(values) / len(values), project=project)


def compute_mr_metrics(
    snap: Snapshot,
    mrs: list[dict[str, Any]],
    projects: dict[int, str],
    now: datetime,
) -> None:
    """Merge requests : ouvertes, brouillons, âge, merges, temps de merge."""
    week_ago = now - timedelta(days=7)
    oldest: dict[str, float] = {}
    merge_times: dict[str, list[float]] = defaultdict(list)

    for mr in mrs:
        project = _project_of(mr, projects)
        state = str(mr.get("state", "unknown"))
        created_at = parse_ts(mr.get("created_at"))
        snap.add("gitlab_mr_merged_7d", 0, project=project)
        if state == "opened":
            snap.add("gitlab_mr_open", 1, project=project)
            snap.add("gitlab_mr_draft", 1 if mr.get("draft") else 0, project=project)
            if created_at:
                age = (now - created_at).total_seconds()
                oldest[project] = max(oldest.get(project, 0.0), age)
        elif state == "merged":
            merged_at = parse_ts(mr.get("merged_at"))
            if merged_at and merged_at >= week_ago:
                snap.add("gitlab_mr_merged_7d", 1, project=project)
            if merged_at and created_at:
                merge_times[project].append((merged_at - created_at).total_seconds())

    for project, age in oldest.items():
        snap.set("gitlab_mr_oldest_open_age_seconds", age, project=project)
    for project, values in merge_times.items():
        snap.set("gitlab_mr_time_to_merge_avg_seconds", sum(values) / len(values), project=project)


def compute_pipeline_metrics(
    snap: Snapshot,
    pipelines_by_project: dict[str, list[dict[str, Any]]],
    now: datetime,
    window_days: int = 7,
) -> None:
    """Pipelines sur la fenêtre : nombre par statut et taux d'échec."""
    since = now - timedelta(days=window_days)
    for project, pipelines in pipelines_by_project.items():
        finished = failed = 0
        for pipe in pipelines:
            created_at = parse_ts(pipe.get("created_at"))
            if created_at is None or created_at < since:
                continue
            status = str(pipe.get("status", "unknown"))
            snap.add("gitlab_pipelines_window", 1, project=project, status=status)
            if status in FINISHED_STATUSES:
                finished += 1
            if status in FAILED_STATUSES:
                failed += 1
        snap.set(
            "gitlab_pipelines_failure_ratio",
            (failed / finished) if finished else 0.0,
            project=project,
        )
