"""Génère grafana/dashboards/gitlab-team-activity.json (source de vérité du dashboard).

Usage : python3 scripts/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DS = {"type": "prometheus", "uid": "prometheus"}
P = 'project=~"$project"'
_next_id = 0


def _id() -> int:
    global _next_id
    _next_id += 1
    return _next_id


def target(
    expr: str, legend: str = "", instant: bool = False, fmt: str = ""
) -> dict[str, Any]:
    t: dict[str, Any] = {
        "datasource": DS,
        "expr": expr,
        "legendFormat": legend,
        "refId": "A",
    }
    if instant:
        t["instant"] = True
        t["range"] = False
    if fmt:
        t["format"] = fmt
    return t


def panel(
    kind: str,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: list[dict[str, Any]],
    desc: str = "",
    unit: str = "short",
    thresholds: list[tuple[str, float | None]] | None = None,
    options: dict[str, Any] | None = None,
    overrides: list[Any] | None = None,
) -> dict[str, Any]:
    for i, t in enumerate(targets):
        t["refId"] = chr(ord("A") + i)
    steps = [{"color": c, "value": v} for c, v in (thresholds or [("green", None)])]
    return {
        "id": _id(),
        "type": kind,
        "title": title,
        "description": desc,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "thresholds": {"mode": "absolute", "steps": steps},
                "color": {"mode": "thresholds" if thresholds else "palette-classic"},
            },
            "overrides": overrides or [],
        },
        "options": options or {},
    }


def row(title: str, y: int) -> dict[str, Any]:
    return {
        "id": _id(),
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }


def stat(
    title: str, expr: str, x: int, y: int, w: int = 4, **kw: Any
) -> dict[str, Any]:
    return panel(
        "stat",
        title,
        x,
        y,
        w,
        4,
        [target(expr, instant=True)],
        options={
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": "background",
            "graphMode": "none",
            "textMode": "value",
        },
        **kw,
    )


def build() -> dict[str, Any]:
    panels: list[dict[str, Any]] = []
    y = 0
    panels.append(row("Vue d'ensemble", y))
    y += 1
    panels += [
        stat("Issues ouvertes", f"sum(gitlab_issues_open{{{P}}})", 0, y),
        stat(
            "Reste à faire (poids)",
            f'sum(gitlab_issues_weight_remaining{{{P}, milestone=~"$milestone"}})',
            4,
            y,
            desc="Somme des poids des issues ouvertes (jalons sélectionnés).",
        ),
        stat(
            "Issues en retard",
            f"sum(gitlab_issues_overdue{{{P}}})",
            8,
            y,
            thresholds=[("green", None), ("orange", 1), ("red", 5)],
        ),
        stat(
            "Issues sans assigné",
            f"sum(gitlab_issues_unassigned{{{P}}})",
            12,
            y,
            thresholds=[("green", None), ("orange", 3), ("red", 10)],
        ),
        stat("MR ouvertes", f"sum(gitlab_mr_open{{{P}}})", 16, y),
        stat(
            "Taux d'échec pipelines (7 j)",
            f'sum(gitlab_pipelines_window{{{P}, status="failed"}}) / '
            f'clamp_min(sum(gitlab_pipelines_window{{{P}, status=~"success|failed|canceled"}}), 1)',
            20,
            y,
            unit="percentunit",
            thresholds=[("green", None), ("orange", 0.15), ("red", 0.3)],
        ),
    ]
    y += 4
    panels += [
        stat("Fermées sur 7 j", f"sum(gitlab_issues_closed_7d{{{P}}})", 0, y),
        stat("Créées sur 7 j", f"sum(gitlab_issues_opened_7d{{{P}}})", 4, y),
        stat(
            "Lead time moyen",
            f"avg(gitlab_issues_lead_time_avg_seconds{{{P}}})",
            8,
            y,
            unit="s",
        ),
        stat(
            "Temps moyen de merge",
            f"avg(gitlab_mr_time_to_merge_avg_seconds{{{P}}})",
            12,
            y,
            unit="s",
        ),
        stat("MR mergées sur 7 j", f"sum(gitlab_mr_merged_7d{{{P}}})", 16, y),
        stat(
            "Collecte GitLab",
            "min(gitlab_exporter_up)",
            20,
            y,
            thresholds=[("red", None), ("green", 1)],
        ),
    ]
    panels[-1]["fieldConfig"]["defaults"]["mappings"] = [
        {"type": "value", "options": {"0": {"text": "KO"}, "1": {"text": "OK"}}}
    ]
    y += 4

    panels.append(row("Work items — état et reste à faire", y))
    y += 1
    panels += [
        panel(
            "bargauge",
            "Issues ouvertes par étape de workflow",
            0,
            y,
            8,
            8,
            [
                target(
                    f"sum by (workflow) (gitlab_issues_open_by_workflow{{{P}}})",
                    "{{workflow}}",
                    instant=True,
                )
            ],
            desc="Basé sur les labels scopés workflow:: (ex. workflow::todo, workflow::doing).",
            options={
                "orientation": "horizontal",
                "displayMode": "gradient",
                "reduceOptions": {"calcs": ["lastNotNull"]},
            },
        ),
        panel(
            "bargauge",
            "Reste à faire par jalon (poids)",
            8,
            y,
            8,
            8,
            [
                target(
                    f'sum by (milestone) (gitlab_issues_weight_remaining{{{P}, milestone=~"$milestone"}})',
                    "{{milestone}}",
                    instant=True,
                )
            ],
            options={
                "orientation": "horizontal",
                "displayMode": "gradient",
                "reduceOptions": {"calcs": ["lastNotNull"]},
            },
        ),
        panel(
            "bargauge",
            "Charge par personne (issues ouvertes)",
            16,
            y,
            8,
            8,
            [
                target(
                    "sort_desc(sum by (assignee) (gitlab_issues_open_by_assignee))",
                    "{{assignee}}",
                    instant=True,
                )
            ],
            options={
                "orientation": "horizontal",
                "displayMode": "gradient",
                "reduceOptions": {"calcs": ["lastNotNull"]},
            },
        ),
    ]
    y += 8
    ts_opts = {
        "legend": {"displayMode": "list", "placement": "bottom"},
        "tooltip": {"mode": "multi"},
    }
    panels += [
        panel(
            "timeseries",
            "Burndown — reste à faire (poids)",
            0,
            y,
            12,
            8,
            [
                target(
                    f'sum by (milestone) (gitlab_issues_weight_remaining{{{P}, milestone=~"$milestone"}})',
                    "{{milestone}}",
                ),
                target(
                    f'sum(gitlab_issues_weight_done{{{P}, milestone=~"$milestone"}})',
                    "Poids fermé (30 j)",
                ),
            ],
            options=ts_opts,
        ),
        panel(
            "timeseries",
            "Débit : issues créées vs fermées (7 j glissants)",
            12,
            y,
            12,
            8,
            [
                target(f"sum(gitlab_issues_opened_7d{{{P}}})", "Créées"),
                target(f"sum(gitlab_issues_closed_7d{{{P}}})", "Fermées"),
                target(f"sum(gitlab_issues_open{{{P}}})", "Stock ouvert"),
            ],
            options=ts_opts,
        ),
    ]
    y += 8

    panels.append(row("Merge requests", y))
    y += 1
    panels += [
        panel(
            "timeseries",
            "MR ouvertes / brouillons",
            0,
            y,
            12,
            8,
            [
                target(f"sum(gitlab_mr_open{{{P}}})", "Ouvertes"),
                target(f"sum(gitlab_mr_draft{{{P}}})", "Brouillons"),
            ],
            options=ts_opts,
        ),
        panel(
            "table",
            "MR : âge de la plus ancienne et temps de merge par projet",
            12,
            y,
            12,
            8,
            [
                target(
                    f"gitlab_mr_oldest_open_age_seconds{{{P}}}",
                    instant=True,
                    fmt="table",
                ),
                target(
                    f"gitlab_mr_time_to_merge_avg_seconds{{{P}}}",
                    instant=True,
                    fmt="table",
                ),
                target(f"gitlab_mr_open{{{P}}}", instant=True, fmt="table"),
            ],
            unit="s",
            options={"showHeader": True},
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "MR ouvertes"},
                    "properties": [{"id": "unit", "value": "short"}],
                }
            ],
        ),
    ]
    panels[-1]["transformations"] = [
        {"id": "merge", "options": {}},
        {
            "id": "organize",
            "options": {
                "excludeByName": {
                    "Time": True,
                    "__name__": True,
                    "instance": True,
                    "job": True,
                },
                "renameByName": {
                    "project": "Projet",
                    "Value #A": "Plus vieille MR ouverte",
                    "Value #B": "Temps moyen de merge",
                    "Value #C": "MR ouvertes",
                },
            },
        },
    ]
    y += 8

    panels.append(row("Pipelines CI/CD", y))
    y += 1
    panels += [
        panel(
            "timeseries",
            "Taux d'échec par projet (7 j glissants)",
            0,
            y,
            12,
            8,
            [target(f"gitlab_pipelines_failure_ratio{{{P}}}", "{{project}}")],
            unit="percentunit",
            options=ts_opts,
        ),
        panel(
            "piechart",
            "Pipelines par statut (7 j)",
            12,
            y,
            6,
            8,
            [
                target(
                    f"sum by (status) (gitlab_pipelines_window{{{P}}})",
                    "{{status}}",
                    instant=True,
                )
            ],
            options={
                "reduceOptions": {"calcs": ["lastNotNull"]},
                "pieType": "donut",
                "legend": {
                    "displayMode": "table",
                    "placement": "right",
                    "values": ["value"],
                },
            },
        ),
        panel(
            "timeseries",
            "Durée du dernier pipeline (branche par défaut & MR)",
            18,
            y,
            6,
            8,
            [
                target(
                    f"max by (project) (gitlab_ci_pipeline_duration_seconds{{{P}}})",
                    "{{project}}",
                )
            ],
            unit="s",
            options=ts_opts,
        ),
    ]
    y += 8
    panels += [
        panel(
            "table",
            "Dernier pipeline par projet / ref",
            0,
            y,
            12,
            9,
            [
                target(
                    f"gitlab_ci_pipeline_status{{{P}}} == 1", instant=True, fmt="table"
                )
            ],
            options={"showHeader": True},
        ),
        panel(
            "table",
            "Jobs actuellement en échec",
            12,
            y,
            12,
            9,
            [
                target(
                    f'gitlab_ci_pipeline_job_status{{{P}, status="failed"}} == 1',
                    instant=True,
                    fmt="table",
                )
            ],
            options={"showHeader": True},
        ),
    ]
    for p in panels[-2:]:
        p["transformations"] = [
            {
                "id": "organize",
                "options": {
                    "excludeByName": {
                        "Time": True,
                        "Value": True,
                        "__name__": True,
                        "instance": True,
                        "job": True,
                    }
                },
            }
        ]
    y += 9
    panels.append(
        panel(
            "timeseries",
            "Exécutions de pipelines (rythme d'intégration)",
            0,
            y,
            24,
            7,
            [
                target(
                    f"sum by (project) (increase(gitlab_ci_pipeline_run_count{{{P}}}[1d]))",
                    "{{project}}",
                )
            ],
            options=ts_opts,
        )
    )
    y += 7

    panels.append(row("Santé de l'instance GitLab", y))
    y += 1
    panels += [
        panel(
            "stat",
            "Cibles GitLab joignables",
            0,
            y,
            6,
            6,
            [target('up{job=~"gitlab.*|workitems-exporter"}', "{{job}}", instant=True)],
            thresholds=[("red", None), ("green", 1)],
            options={
                "reduceOptions": {"calcs": ["lastNotNull"]},
                "colorMode": "background",
                "textMode": "name",
                "graphMode": "none",
            },
        ),
        panel(
            "timeseries",
            "Requêtes HTTP Rails par statut",
            6,
            y,
            9,
            6,
            [
                target(
                    'sum by (status) (rate(http_requests_total{job=~"gitlab-rails|gitlab-puma"}[$__rate_interval]))',
                    "{{status}}",
                )
            ],
            unit="reqps",
            options=ts_opts,
        ),
        panel(
            "timeseries",
            "Sidekiq : jobs en cours / en échec",
            15,
            y,
            9,
            6,
            [
                target("sum(sidekiq_running_jobs)", "En cours"),
                target(
                    "sum(rate(sidekiq_jobs_failed_total[$__rate_interval]))",
                    "Échecs /s",
                ),
            ],
            options=ts_opts,
        ),
    ]

    return {
        "uid": "gitlab-team-activity",
        "title": "GitLab — Activité de l'équipe",
        "tags": ["gitlab", "kpi", "devops"],
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "editable": True,
        "graphTooltip": 1,
        "time": {"from": "now-30d", "to": "now"},
        "refresh": "5m",
        "templating": {
            "list": [
                {
                    "name": "project",
                    "label": "Projet",
                    "type": "query",
                    "datasource": DS,
                    "query": {
                        "query": "label_values(gitlab_issues_open, project)",
                        "refId": "q1",
                    },
                    "definition": "label_values(gitlab_issues_open, project)",
                    "includeAll": True,
                    "multi": True,
                    "allValue": ".*",
                    "refresh": 2,
                    "current": {"text": "All", "value": "$__all"},
                },
                {
                    "name": "milestone",
                    "label": "Jalon",
                    "type": "query",
                    "datasource": DS,
                    "query": {
                        "query": 'label_values(gitlab_issues_weight_remaining{project=~"$project"}, milestone)',
                        "refId": "q2",
                    },
                    "definition": "label_values(gitlab_issues_weight_remaining, milestone)",
                    "includeAll": True,
                    "multi": True,
                    "allValue": ".*",
                    "refresh": 2,
                    "current": {"text": "All", "value": "$__all"},
                },
            ]
        },
        "annotations": {"list": []},
        "panels": panels,
    }


if __name__ == "__main__":
    out = (
        Path(__file__).resolve().parent.parent
        / "grafana/dashboards/gitlab-team-activity.json"
    )
    out.write_text(
        json.dumps(build(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Dashboard écrit : {out}")
