from datetime import UTC, datetime

import pytest

from workitems_exporter.gitlab_client import GitLabClient
from workitems_exporter.metrics import (
    Snapshot,
    compute_issue_metrics,
    compute_mr_metrics,
    compute_pipeline_metrics,
    parse_ts,
)

NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)
PROJECTS = {1: "team/api", 2: "team/web"}


def issue(**kw):
    base = {
        "project_id": 1,
        "state": "opened",
        "labels": [],
        "assignees": [],
        "created_at": "2026-10-01T10:00:00Z",
    }
    base.update(kw)
    return base


def test_weight_falls_back_to_scoped_label_on_ce():
    snap = Snapshot()
    issues = [
        issue(weight=None, labels=["weight::5", "workflow::todo"], milestone={"title": "S45"}),
        issue(weight=2, labels=["weight::8"], milestone={"title": "S45"}),  # natif prioritaire
        issue(weight=None, labels=["weight::abc"], milestone={"title": "S45"}),  # ignoré
        issue(state="closed", weight=None, labels=["weight::3"], milestone={"title": "S45"}),
    ]
    compute_issue_metrics(snap, issues, PROJECTS, NOW)
    assert snap.get("gitlab_issues_weight_remaining", project="team/api", milestone="S45") == 7
    assert snap.get("gitlab_issues_weight_done", project="team/api", milestone="S45") == 3


def test_parse_ts_handles_z_and_none():
    assert parse_ts(None) is None
    assert parse_ts("2026-11-05T12:00:00Z") == NOW


def test_open_issues_workflow_weight_and_assignees():
    snap = Snapshot()
    issues = [
        issue(
            labels=["workflow::doing", "bug"],
            weight=3,
            milestone={"title": "S45"},
            assignees=[{"username": "alice"}],
        ),
        issue(labels=["workflow::doing"], weight=2, milestone={"title": "S45"}),
        issue(project_id=2, weight=None),
    ]
    compute_issue_metrics(snap, issues, PROJECTS, NOW)
    assert snap.get("gitlab_issues_open", project="team/api") == 2
    assert snap.get("gitlab_issues_open_by_workflow", project="team/api", workflow="doing") == 2
    assert snap.get("gitlab_issues_open_by_workflow", project="team/web", workflow="none") == 1
    assert snap.get("gitlab_issues_weight_remaining", project="team/api", milestone="S45") == 5
    assert snap.get("gitlab_issues_weight_remaining", project="team/web", milestone="none") == 0
    assert snap.get("gitlab_issues_unassigned", project="team/api") == 1
    assert snap.get("gitlab_issues_open_by_assignee", assignee="alice") == 1


def test_overdue_and_series_exist_at_zero():
    snap = Snapshot()
    compute_issue_metrics(
        snap, [issue(due_date="2026-11-01"), issue(due_date="2026-12-01")], PROJECTS, NOW
    )
    assert snap.get("gitlab_issues_overdue", project="team/api") == 1
    snap2 = Snapshot()
    compute_issue_metrics(snap2, [issue(project_id=2)], PROJECTS, NOW)
    assert snap2.get("gitlab_issues_overdue", project="team/web") == 0


def test_closed_issues_throughput_and_lead_time():
    snap = Snapshot()
    issues = [
        issue(
            state="closed",
            created_at="2026-11-01T12:00:00Z",
            closed_at="2026-11-03T12:00:00Z",
            weight=5,
            milestone={"title": "S45"},
        ),
        issue(state="closed", created_at="2026-10-01T12:00:00Z", closed_at="2026-10-05T12:00:00Z"),
        issue(created_at="2026-11-04T00:00:00Z"),
    ]
    compute_issue_metrics(snap, issues, PROJECTS, NOW)
    assert snap.get("gitlab_issues_closed_7d", project="team/api") == 1
    assert snap.get("gitlab_issues_opened_7d", project="team/api") == 2
    assert snap.get("gitlab_issues_weight_done", project="team/api", milestone="S45") == 5
    # (2 j + 4 j) / 2 = 3 jours
    assert snap.get("gitlab_issues_lead_time_avg_seconds", project="team/api") == 3 * 86400


def test_project_from_references_when_unknown_id():
    snap = Snapshot()
    compute_issue_metrics(
        snap, [issue(project_id=99, references={"full": "other/x#4"})], PROJECTS, NOW
    )
    assert snap.get("gitlab_issues_open", project="other/x") == 1
    snap2 = Snapshot()
    compute_issue_metrics(snap2, [issue(project_id=None)], PROJECTS, NOW)
    assert snap2.get("gitlab_issues_open", project="unknown") == 1


def test_merge_request_metrics():
    snap = Snapshot()
    mrs = [
        {"project_id": 1, "state": "opened", "draft": True, "created_at": "2026-11-03T12:00:00Z"},
        {"project_id": 1, "state": "opened", "draft": False, "created_at": "2026-11-05T00:00:00Z"},
        {
            "project_id": 1,
            "state": "merged",
            "created_at": "2026-11-01T12:00:00Z",
            "merged_at": "2026-11-02T12:00:00Z",
        },
        {
            "project_id": 2,
            "state": "merged",
            "created_at": "2026-10-01T12:00:00Z",
            "merged_at": "2026-10-02T12:00:00Z",
        },
    ]
    compute_mr_metrics(snap, mrs, PROJECTS, NOW)
    assert snap.get("gitlab_mr_open", project="team/api") == 2
    assert snap.get("gitlab_mr_draft", project="team/api") == 1
    assert snap.get("gitlab_mr_oldest_open_age_seconds", project="team/api") == 2 * 86400
    assert snap.get("gitlab_mr_merged_7d", project="team/api") == 1
    assert snap.get("gitlab_mr_merged_7d", project="team/web") == 0
    assert snap.get("gitlab_mr_time_to_merge_avg_seconds", project="team/web") == 86400


def test_pipeline_failure_ratio_ignores_running_and_old():
    snap = Snapshot()
    pipes = {
        "team/api": [
            {"status": "success", "created_at": "2026-11-04T10:00:00Z"},
            {"status": "failed", "created_at": "2026-11-04T11:00:00Z"},
            {"status": "failed", "created_at": "2026-11-04T12:00:00Z"},
            {"status": "canceled", "created_at": "2026-11-04T12:00:00Z"},
            {"status": "running", "created_at": "2026-11-05T11:00:00Z"},
            {"status": "failed", "created_at": "2026-10-01T11:00:00Z"},
            {"status": "failed"},
        ],
        "team/web": [],
    }
    compute_pipeline_metrics(snap, pipes, NOW)
    assert snap.get("gitlab_pipelines_failure_ratio", project="team/api") == pytest.approx(0.5)
    assert snap.get("gitlab_pipelines_window", project="team/api", status="failed") == 2
    assert snap.get("gitlab_pipelines_window", project="team/api", status="running") == 1
    assert snap.get("gitlab_pipelines_failure_ratio", project="team/web") == 0.0


def test_client_rejects_non_http_url():
    with pytest.raises(ValueError):
        GitLabClient("file:///etc/passwd", "t")


def test_group_path_is_url_encoded():
    assert GitLabClient("http://x", "t").group_path("a/b") == "/groups/a%2Fb"
