"""Exporter Prometheus : activité d'équipe GitLab (work items, MR, pipelines).

Les données sont rafraîchies en arrière-plan toutes les REFRESH_SECONDS ;
chaque scrape Prometheus lit le dernier instantané (pas d'appel API par scrape).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from prometheus_client import start_http_server
from prometheus_client.core import REGISTRY, GaugeMetricFamily
from prometheus_client.registry import Collector

from .gitlab_client import GitLabClient, GitLabError
from .metrics import (
    Snapshot,
    compute_issue_metrics,
    compute_mr_metrics,
    compute_pipeline_metrics,
)

LOG = logging.getLogger("workitems_exporter")

HELP = {
    "gitlab_issues_open": "Issues ouvertes",
    "gitlab_issues_open_by_workflow": "Issues ouvertes par étape (label scopé workflow::)",
    "gitlab_issues_open_by_milestone": "Issues ouvertes par jalon",
    "gitlab_issues_open_by_assignee": "Issues ouvertes par personne assignée",
    "gitlab_issues_unassigned": "Issues ouvertes sans assigné",
    "gitlab_issues_overdue": "Issues ouvertes dont l'échéance est dépassée",
    "gitlab_issues_weight_remaining": "Reste à faire : poids des issues ouvertes",
    "gitlab_issues_weight_done": "Poids des issues fermées (fenêtre d'historique)",
    "gitlab_issues_opened_7d": "Issues créées sur 7 jours",
    "gitlab_issues_closed_7d": "Issues fermées sur 7 jours",
    "gitlab_issues_lead_time_avg_seconds": "Lead time moyen création -> fermeture",
    "gitlab_mr_open": "Merge requests ouvertes",
    "gitlab_mr_draft": "Merge requests ouvertes en brouillon",
    "gitlab_mr_oldest_open_age_seconds": "Âge de la plus vieille MR ouverte",
    "gitlab_mr_merged_7d": "MR mergées sur 7 jours",
    "gitlab_mr_time_to_merge_avg_seconds": "Temps moyen création -> merge",
    "gitlab_pipelines_window": "Pipelines sur la fenêtre, par statut",
    "gitlab_pipelines_failure_ratio": "Taux d'échec des pipelines terminés sur la fenêtre",
}


class SnapshotCollector(Collector):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = Snapshot()
        self.last_success = 0.0
        self.last_duration = 0.0
        self.up = 0.0

    def update(self, snap: Snapshot, duration: float) -> None:
        with self._lock:
            self._snapshot = snap
            self.last_success = time.time()
            self.last_duration = duration
            self.up = 1.0

    def mark_failed(self) -> None:
        with self._lock:
            self.up = 0.0

    def collect(self) -> Iterator[GaugeMetricFamily]:
        with self._lock:
            snap, up = self._snapshot, self.up
            last, dur = self.last_success, self.last_duration
        for name, series in snap.gauges.items():
            label_names = [k for k, _ in next(iter(series))] if series else []
            fam = GaugeMetricFamily(name, HELP.get(name, name), labels=label_names)
            for labels, value in series.items():
                fam.add_metric([v for _, v in labels], value)
            yield fam
        yield GaugeMetricFamily(
            "gitlab_exporter_up", "1 si la dernière collecte a réussi", value=up
        )
        yield GaugeMetricFamily(
            "gitlab_exporter_last_success_timestamp_seconds",
            "Horodatage de la dernière collecte réussie",
            value=last,
        )
        yield GaugeMetricFamily(
            "gitlab_exporter_collect_duration_seconds", "Durée de la dernière collecte", value=dur
        )


def collect_once(
    client: GitLabClient, group: str, history_days: int, workflow_prefix: str
) -> Snapshot:
    now = datetime.now(UTC)
    since = (now - timedelta(days=history_days)).isoformat()
    gpath = client.group_path(group)

    projects_raw = client.paginate(
        f"{gpath}/projects", include_subgroups="true", archived="false", simple="true"
    )
    projects = {int(p["id"]): str(p["path_with_namespace"]) for p in projects_raw}

    open_issues = client.paginate(f"{gpath}/issues", state="opened", scope="all")
    closed_issues = client.paginate(
        f"{gpath}/issues", state="closed", scope="all", updated_after=since
    )
    mrs = client.paginate(f"{gpath}/merge_requests", state="opened", scope="all")
    mrs += client.paginate(
        f"{gpath}/merge_requests", state="merged", scope="all", updated_after=since
    )

    pipelines: dict[str, list[dict[str, object]]] = {}
    pipe_since = (now - timedelta(days=7)).isoformat()
    for pid, name in projects.items():
        pipelines[name] = client.paginate(f"/projects/{pid}/pipelines", updated_after=pipe_since)

    snap = Snapshot()
    compute_issue_metrics(snap, open_issues + closed_issues, projects, now, workflow_prefix)
    compute_mr_metrics(snap, mrs, projects, now)
    compute_pipeline_metrics(snap, pipelines, now)
    return snap


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
    )
    url = os.environ.get("GITLAB_URL", "http://gitlab")
    token = os.environ.get("GITLAB_TOKEN", "")
    group = os.environ.get("GITLAB_GROUP", "")
    if not token or not group:
        raise SystemExit("GITLAB_TOKEN et GITLAB_GROUP sont obligatoires")
    port = int(os.getenv("EXPORTER_PORT", "9200"))
    refresh = int(os.getenv("REFRESH_SECONDS", "300"))
    history = int(os.getenv("HISTORY_DAYS", "30"))
    prefix = os.getenv("WORKFLOW_LABEL_PREFIX", "workflow::")

    client = GitLabClient(url, token)
    collector = SnapshotCollector()
    REGISTRY.register(collector)
    start_http_server(port)
    LOG.info("Exporter démarré sur :%d (groupe %s)", port, group)

    while True:
        start = time.monotonic()
        try:
            collector.update(collect_once(client, group, history, prefix), time.monotonic() - start)
            LOG.info("Collecte OK en %.1fs", time.monotonic() - start)
        except (GitLabError, KeyError, ValueError) as exc:
            collector.mark_failed()
            LOG.error("Collecte en échec : %s", exc)
        time.sleep(refresh)


if __name__ == "__main__":
    main()
