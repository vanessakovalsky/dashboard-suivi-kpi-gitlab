#!/usr/bin/env python3
"""Simulateur de métriques GitLab pour le dashboard « GitLab — Activité de l'équipe ».

Aucun GitLab nécessaire, bibliothèque standard Python uniquement (compatible airgap).
Les valeurs sont une fonction déterministe du temps : le mode live et l'historique
généré se raccordent exactement, et les compteurs restent monotones même après un
redémarrage.

Deux modes :

  serve     expose /metrics (à scraper par Prometheus avec honor_labels: true)
            python3 simulate_gitlab_metrics.py serve --port 9200

  backfill  génère un fichier OpenMetrics d'historique pour promtool
            python3 simulate_gitlab_metrics.py backfill --days 30 --out kpi.om
            promtool tsdb create-blocks-from openmetrics --max-block-duration=24h kpi.om <data>

Le scénario simulé : 3 projets, des sprints de 14 jours avec burndown et ajout de
périmètre, un workflow todo/doing/review, des retards en fin de sprint, des MR, des
pipelines avec des taux d'échec différents par projet et des incidents ponctuels, ainsi
que le trafic de l'instance GitLab.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import random
import re
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EPOCH = datetime(2026, 1, 5, tzinfo=UTC).timestamp()  # un lundi : début des sprints
HOUR = 3600.0
DAY = 24 * HOUR
SPRINT = 14 * DAY
AVG_WEIGHT = 2.8
INSTANCE = "simulator"
JOB_WORKITEMS = "workitems-exporter"
JOB_GCPE = "gitlab-ci-pipelines-exporter"

# projet -> (vélocité en points de poids par sprint, taux d'échec de base des pipelines)
PROJECTS: dict[str, tuple[float, float]] = {
    "equipe-demo/api": (60.0, 0.12),
    "equipe-demo/web": (80.0, 0.28),
    "equipe-demo/mobile": (40.0, 0.08),
}
ASSIGNEES = ["alice", "bob", "chloe", "david"]
PIPELINE_STATUSES = [
    "success",
    "failed",
    "running",
    "pending",
    "canceled",
    "skipped",
    "manual",
]
JOB_STATUSES = ["success", "failed", "running", "pending", "skipped"]
JOBS = [("build", "build"), ("test", "test"), ("deploy", "deploy")]

Labels = tuple[tuple[str, str], ...]
Sample = tuple[str, Labels, float]
COUNTERS = {
    "gitlab_ci_pipeline_run_count",
    "http_requests_total",
    "sidekiq_jobs_failed_total",
}


# --------------------------------------------------------------------------- bruit
def _rng(*key: object) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, key)).encode()).digest()
    # Aléatoire non cryptographique volontaire : bruit déterministe de démo.
    return random.Random(int.from_bytes(digest[:8], "big"))  # nosec B311


def noise(key: object, t: float, period: float) -> float:
    """Bruit lisse dans [-1, 1] (interpolation cosinus entre des points aléatoires)."""
    pos = (t - EPOCH) / period
    bucket = math.floor(pos)
    frac = pos - bucket
    a = _rng(key, bucket).uniform(-1, 1)
    b = _rng(key, bucket + 1).uniform(-1, 1)
    w = (1 - math.cos(math.pi * frac)) / 2
    return a * (1 - w) + b * w


def sprint_at(t: float) -> tuple[int, float, str, str, str]:
    """(index, avancement 0..1, jalon précédent, jalon en cours, jalon suivant)."""
    k = math.floor((t - EPOCH) / SPRINT)
    start = EPOCH + k * SPRINT

    def title(s: float) -> str:
        return "Sprint " + datetime.fromtimestamp(s, UTC).strftime("%V")

    return (
        k,
        (t - start) / SPRINT,
        title(start - SPRINT),
        title(start),
        title(start + SPRINT),
    )


def _lbl(**labels: str) -> Labels:
    return tuple(sorted({"instance": INSTANCE, **labels}.items()))


# --------------------------------------------------------------------------- modèle
def workitem_samples(t: float) -> list[Sample]:
    out: list[Sample] = []
    by_assignee: dict[str, float] = defaultdict(float)

    def add(name: str, value: float, **labels: str) -> None:
        out.append((name, _lbl(job=JOB_WORKITEMS, **labels), float(value)))

    for project, (velocity, base_fail) in PROJECTS.items():
        k, f, prev_ms, cur_ms, next_ms = sprint_at(t)
        scope = velocity * (1 + 0.15 * _rng(project, "scope", k).uniform(-1, 1))
        scope_now = scope * (
            1 + 0.12 * min(1.0, 2 * f)
        )  # ajout de périmètre en cours de sprint
        progress = min(1.0, f**1.15 * (1.05 + 0.08 * noise((project, "vel"), t, DAY)))
        done = min(scope_now * 0.95, scope * progress)
        rem_cur = max(0.0, scope_now - done)
        rem_next = velocity * (0.5 + 0.35 * f)
        rem_none = velocity * 0.35 * (1 + 0.2 * noise((project, "backlog"), t, 3 * DAY))

        open_cur = round(rem_cur / AVG_WEIGHT)
        open_next = round(rem_next / AVG_WEIGHT)
        open_none = round(rem_none / AVG_WEIGHT)
        open_total = open_cur + open_next + open_none

        todo_cur = round(open_cur * 0.7 * (1 - f))
        review = round(open_cur * (0.12 + 0.18 * f))
        doing = max(0, open_cur - todo_cur - review)

        add("gitlab_issues_open", open_total, project=project)
        for workflow, n in (
            ("todo", todo_cur + open_next),
            ("doing", doing),
            ("review", review),
            ("none", open_none),
        ):
            add("gitlab_issues_open_by_workflow", n, project=project, workflow=workflow)
        for ms, w, n in (
            (cur_ms, rem_cur, open_cur),
            (next_ms, rem_next, open_next),
            ("none", rem_none, open_none),
        ):
            add(
                "gitlab_issues_weight_remaining",
                round(w),
                project=project,
                milestone=ms,
            )
            add("gitlab_issues_open_by_milestone", n, project=project, milestone=ms)
        prev_scope = velocity * (
            1 + 0.15 * _rng(project, "scope", k - 1).uniform(-1, 1)
        )
        add("gitlab_issues_weight_done", round(done), project=project, milestone=cur_ms)
        add(
            "gitlab_issues_weight_done",
            round(prev_scope * 0.95),
            project=project,
            milestone=prev_ms,
        )

        unassigned = open_none // 2 + round(open_next * 0.25)
        add("gitlab_issues_unassigned", unassigned, project=project)
        late = max(0.0, (f - 0.6) / 0.4)
        overdue = round(open_cur * 0.3 * late) + (
            1 if noise((project, "od"), t, DAY) > 0.6 else 0
        )
        add("gitlab_issues_overdue", overdue, project=project)

        weights = [_rng(project, "assign", k, a).uniform(0.5, 1.5) for a in ASSIGNEES]
        assigned = open_total - unassigned
        for a, w in zip(ASSIGNEES, weights, strict=True):
            by_assignee[a] += assigned * w / sum(weights)

        closed_7d = round(
            velocity / AVG_WEIGHT / 2 * (1 + 0.25 * noise((project, "c7"), t, DAY))
        )
        opened_7d = round(closed_7d * (1.05 + 0.2 * noise((project, "o7"), t, DAY)))
        add("gitlab_issues_closed_7d", closed_7d, project=project)
        add("gitlab_issues_opened_7d", opened_7d, project=project)
        add(
            "gitlab_issues_lead_time_avg_seconds",
            (3.5 + 1.5 * noise((project, "lt"), t, 2 * DAY)) * DAY,
            project=project,
        )

        scale = velocity / 60
        add(
            "gitlab_mr_open",
            round(scale * (5 + 2 * noise((project, "mr"), t, HOUR * 8))),
            project=project,
        )
        add(
            "gitlab_mr_draft",
            round(scale * (1.5 + noise((project, "dr"), t, DAY))),
            project=project,
        )
        add(
            "gitlab_mr_oldest_open_age_seconds",
            (4 + 3 * noise((project, "age"), t, 2 * DAY)) * DAY,
            project=project,
        )
        add(
            "gitlab_mr_merged_7d",
            round(scale * (12 + 4 * noise((project, "mg"), t, DAY))),
            project=project,
        )
        add(
            "gitlab_mr_time_to_merge_avg_seconds",
            (0.9 + 0.5 * noise((project, "ttm"), t, 2 * DAY)) * DAY,
            project=project,
        )

        ratio = failure_ratio(project, base_fail, t)
        total = round(velocity * 0.9 * (1 + 0.2 * noise((project, "pt"), t, DAY)))
        running = max(0, round(1 + noise((project, "run"), t, HOUR)))
        pending = 1 if noise((project, "pend"), t, HOUR) > 0.5 else 0
        finished = total - running - pending
        failed = round(finished * ratio)
        canceled = round(finished * 0.04)
        for status, n in (
            ("success", finished - failed - canceled),
            ("failed", failed),
            ("canceled", canceled),
            ("running", running),
            ("pending", pending),
        ):
            add("gitlab_pipelines_window", n, project=project, status=status)
        add(
            "gitlab_pipelines_failure_ratio",
            failed / finished if finished else 0.0,
            project=project,
        )

    for a, n in by_assignee.items():
        add("gitlab_issues_open_by_assignee", round(n), assignee=a)
    add("gitlab_exporter_up", 1)
    add("gitlab_exporter_last_success_timestamp_seconds", math.floor(t / 300) * 300)
    add("gitlab_exporter_collect_duration_seconds", 4 + 2 * noise("dur", t, HOUR))
    return out


def failure_ratio(project: str, base: float, t: float) -> float:
    incident = max(0.0, noise((project, "incident"), t, 3 * DAY) - 0.55) / 0.45
    return min(
        0.9, max(0.0, base + 0.05 * noise((project, "fr"), t, DAY) + 0.35 * incident)
    )


def gcpe_samples(t: float) -> list[Sample]:
    """Séries au format de mvisonneau/gitlab-ci-pipelines-exporter."""
    out: list[Sample] = []

    def add(name: str, value: float, **labels: str) -> None:
        out.append((name, _lbl(job=JOB_GCPE, **labels), float(value)))

    for project, (velocity, base_fail) in PROJECTS.items():
        period = 7 * DAY / (velocity * 0.6)  # intervalle moyen entre pipelines sur main
        mr_bucket = math.floor((t - EPOCH) / (3 * DAY))
        refs = [("main", "branch", EPOCH, period)]
        for i in range(2):
            n = 100 + mr_bucket * 2 + i + list(PROJECTS).index(project) * 1000
            refs.append(
                (
                    f"refs/merge-requests/{n}/head",
                    "merge-request",
                    EPOCH + mr_bucket * 3 * DAY,
                    period * 2.5,
                )
            )
        ratio = failure_ratio(project, base_fail, t)
        for ref, kind, origin, per in refs:
            runs = math.floor((t - origin) / per)
            frac = (t - origin) / per - runs
            r = _rng(project, ref, runs)
            last = "failed" if r.random() < ratio else "success"
            status = "running" if frac < 0.06 else last
            add(
                "gitlab_ci_pipeline_run_count",
                runs,
                project=project,
                ref=ref,
                kind=kind,
            )
            add(
                "gitlab_ci_pipeline_duration_seconds",
                round(r.uniform(150, 540)),
                project=project,
                ref=ref,
                kind=kind,
            )
            for s in PIPELINE_STATUSES:
                add(
                    "gitlab_ci_pipeline_status",
                    1 if s == status else 0,
                    project=project,
                    ref=ref,
                    kind=kind,
                    status=s,
                )
            job_status = {
                "build": "running" if status == "running" else "success",
                "test": "pending" if status == "running" else status,
                "deploy": "success"
                if status == "success" and kind == "branch"
                else "skipped",
            }
            if kind != "branch":
                continue  # statut des jobs : branche par défaut uniquement (volume)
            for stage, job_name in JOBS:
                for s in JOB_STATUSES:
                    add(
                        "gitlab_ci_pipeline_job_status",
                        1 if s == job_status[job_name] else 0,
                        project=project,
                        ref=ref,
                        kind=kind,
                        stage=stage,
                        job_name=job_name,
                        status=s,
                    )
    return out


def _daily_counter(rate: float, t: float, phase: float = 0.0) -> float:
    """Intégrale d'un débit avec cycle jour/nuit (toujours croissante)."""
    omega = 2 * math.pi / DAY
    x = t - EPOCH
    amp = 0.7
    return rate * (x - amp / omega * (math.cos(omega * x + phase) - math.cos(phase)))


def health_samples(t: float, with_up: bool) -> list[Sample]:
    out: list[Sample] = []
    phase = -math.pi / 2 - 2 * math.pi * 14 / 24  # pic vers 14 h UTC
    for status, rate in (
        ("200", 6.0),
        ("302", 0.6),
        ("304", 0.8),
        ("404", 0.15),
        ("500", 0.02),
    ):
        out.append(
            (
                "http_requests_total",
                _lbl(job="gitlab-rails", status=status),
                round(_daily_counter(rate, t, phase)),
            )
        )
    out.append(
        (
            "sidekiq_jobs_failed_total",
            _lbl(job="gitlab-sidekiq"),
            round(_daily_counter(0.01, t, phase)),
        )
    )
    out.append(
        (
            "sidekiq_running_jobs",
            _lbl(job="gitlab-sidekiq"),
            max(
                0,
                round(
                    4
                    + 3 * math.sin(2 * math.pi * (t - EPOCH) / DAY + phase)
                    + 2 * noise("sk", t, HOUR)
                ),
            ),
        )
    )
    if with_up:
        for job in (
            "gitlab-rails",
            "gitlab-puma",
            "gitlab-sidekiq",
            JOB_GCPE,
            JOB_WORKITEMS,
        ):
            out.append(("up", _lbl(job=job), 1.0))
    return out


def all_samples(t: float, with_up: bool = False) -> list[Sample]:
    return workitem_samples(t) + gcpe_samples(t) + health_samples(t, with_up)


# --------------------------------------------------------------------------- rendu
def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _series(name: str, labels: Labels) -> str:
    inner = ",".join(f'{k}="{_esc(v)}"' for k, v in labels)
    return f"{name}{{{inner}}}"


def _fmt(v: float) -> str:
    return str(int(v)) if v.is_integer() else repr(v)


def render_text(samples: list[Sample]) -> str:
    """Format d'exposition Prometheus 0.0.4 (mode serve)."""
    families: dict[str, list[Sample]] = defaultdict(list)
    for s in samples:
        families[s[0]].append(s)
    lines = []
    for name, rows in families.items():
        lines.append(f"# TYPE {name} {'counter' if name in COUNTERS else 'gauge'}")
        lines += [f"{_series(name, lbl)} {_fmt(v)}" for _, lbl, v in rows]
    return "\n".join(lines) + "\n"


def backfill(out_path: str, start: float, end: float, step: float) -> int:
    """Écrit un fichier OpenMetrics (familles contiguës, séries triées dans le temps)."""
    series: dict[str, dict[Labels, list[tuple[int, float]]]] = defaultdict(dict)
    t = start
    while t <= end:
        for name, labels, value in all_samples(t, with_up=True):
            series[name].setdefault(labels, []).append((int(t), value))
        t += step
    count = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        for name, by_labels in series.items():
            # Type "unknown" : évite les contraintes de suffixe _total d'OpenMetrics.
            fh.write(f"# TYPE {name} unknown\n")
            for labels, points in by_labels.items():
                prefix = _series(name, labels)
                for ts, value in points:
                    fh.write(f"{prefix} {_fmt(value)} {ts}\n")
                    count += 1
        fh.write("# EOF\n")
    return count


def parse_step(value: str) -> float:
    m = re.fullmatch(r"(\d+)([smhd])", value)
    if not m:
        raise argparse.ArgumentTypeError("format attendu : 30s, 15m, 1h, 1d")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.split("?")[0] != "/metrics":
            body, ctype, code = b"Voir /metrics\n", "text/plain", 200
        else:
            body = render_text(all_samples(time.time(), with_up=True)).encode()
            ctype, code = "text/plain; version=0.0.4; charset=utf-8", 200
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    srv = sub.add_parser("serve", help="expose /metrics en continu")
    srv.add_argument("--host", default="0.0.0.0")  # nosec B104 - à scraper
    srv.add_argument("--port", type=int, default=9200)
    bf = sub.add_parser(
        "backfill", help="génère un historique OpenMetrics pour promtool"
    )
    bf.add_argument("--days", type=int, default=30)
    bf.add_argument(
        "--step",
        type=parse_step,
        default=parse_step("5m"),
        help="5m max : au-delà, trous dans Grafana (lookback Prometheus de 5 min)",
    )
    bf.add_argument("--out", default="gitlab-kpi-backfill.om")
    args = parser.parse_args(argv)

    if args.cmd == "serve":
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        print(
            f"Simulateur sur http://{args.host}:{args.port}/metrics (Ctrl+C pour arrêter)"
        )
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    end = math.floor(time.time() / args.step) * args.step - args.step
    start = end - args.days * DAY
    n = backfill(args.out, start, end, args.step)
    print(
        f"{n} échantillons écrits dans {args.out} "
        f"({datetime.fromtimestamp(start, UTC):%Y-%m-%d} -> "
        f"{datetime.fromtimestamp(end, UTC):%Y-%m-%d %H:%M} UTC)"
    )
    print(
        "Import : promtool tsdb create-blocks-from openmetrics --max-block-duration=24h "
        f"{args.out} <dossier data Prometheus>"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
