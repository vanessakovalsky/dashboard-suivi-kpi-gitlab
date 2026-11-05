"""Tests du simulateur scripts/simulate_gitlab_metrics.py (stdlib, hors package)."""

import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "simulate_gitlab_metrics", ROOT / "scripts/simulate_gitlab_metrics.py"
)
assert _spec and _spec.loader
sim = importlib.util.module_from_spec(_spec)
sys.modules["simulate_gitlab_metrics"] = sim
_spec.loader.exec_module(sim)

T0 = 1_793_000_000.0  # novembre 2026


def _index(samples):
    return {(n, lbl): v for n, lbl, v in samples}


def test_every_dashboard_metric_is_simulated():
    dash = json.loads((ROOT / "grafana/dashboards/gitlab-team-activity.json").read_text())
    exprs = " ".join(t["expr"] for p in dash["panels"] for t in p.get("targets", []))
    used = set(re.findall(r"\b(gitlab_[a-z_]+|http_requests_total|sidekiq_[a-z_]+)\b", exprs))
    produced = {n for n, _, _ in sim.all_samples(T0, with_up=True)}
    assert used - produced == set()


def test_deterministic_in_time():
    assert sim.all_samples(T0) == sim.all_samples(T0)


def test_counters_are_monotonic():
    a, b = _index(sim.all_samples(T0)), _index(sim.all_samples(T0 + 3600))
    for (name, lbl), v in a.items():
        if name in sim.COUNTERS and (name, lbl) in b and "merge-requests" not in str(lbl):
            assert b[(name, lbl)] >= v, name


def test_values_are_sane():
    for t in (T0 + i * 7919 for i in range(200)):
        idx = _index(sim.all_samples(t))
        for (name, _lbl), v in idx.items():
            assert v >= 0, name
            if name == "gitlab_pipelines_failure_ratio":
                assert 0 <= v <= 1
        statuses = [
            v
            for (n, lbl), v in idx.items()
            if n == "gitlab_ci_pipeline_status"
            and dict(lbl)["project"] == "equipe-demo/api"
            and dict(lbl)["ref"] == "main"
        ]
        assert sum(statuses) == 1  # un seul statut actif par ref


def test_burndown_decreases_within_sprint():
    k, _f, _p, cur, _n = sim.sprint_at(T0)
    start = sim.EPOCH + k * sim.SPRINT
    key = (
        "gitlab_issues_weight_remaining",
        sim._lbl(job=sim.JOB_WORKITEMS, project="equipe-demo/api", milestone=cur),
    )
    early = _index(sim.all_samples(start + 2 * sim.DAY))[key]
    late = _index(sim.all_samples(start + 13 * sim.DAY))[key]
    assert late < early / 2


def test_render_text_format():
    text = sim.render_text(sim.all_samples(T0, with_up=True))
    assert "# TYPE http_requests_total counter" in text
    assert re.search(
        r'^gitlab_issues_open\{instance="simulator",job="workitems-exporter",'
        r'project="equipe-demo/api"\} \d+$',
        text,
        re.M,
    )


def test_backfill_openmetrics(tmp_path):
    out = tmp_path / "kpi.om"
    n = sim.backfill(str(out), T0, T0 + 3600, 300)
    lines = out.read_text().splitlines()
    assert lines[-1] == "# EOF"
    assert n == sum(1 for line in lines if not line.startswith("#"))
    families = [line.split()[2] for line in lines if line.startswith("# TYPE")]
    assert len(families) == len(set(families))  # familles contiguës, non répétées


def test_parse_step():
    assert sim.parse_step("5m") == 300
    assert sim.parse_step("1h") == 3600
