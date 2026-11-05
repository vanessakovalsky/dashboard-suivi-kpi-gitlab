"""Test fonctionnel : faux GitLab HTTP -> collecte -> endpoint /metrics réel."""

import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from workitems_exporter.__main__ import SnapshotCollector, collect_once
from workitems_exporter.gitlab_client import GitLabClient, GitLabError

TOKEN = "glpat-fixture-token"  # fixture synthétique, pas un vrai secret


def _routes():
    p1 = {"id": 1, "path_with_namespace": "demo/api"}
    open_issues = [
        {
            "project_id": 1,
            "state": "opened",
            "weight": 3,
            "labels": ["workflow::todo"],
            "assignees": [],
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]
    return {
        "/api/v4/groups/demo/projects": [[p1]],
        "/api/v4/groups/demo/issues|opened": [open_issues],
        "/api/v4/groups/demo/issues|closed": [[]],
        "/api/v4/groups/demo/merge_requests|opened": [[]],
        "/api/v4/groups/demo/merge_requests|merged": [[]],
        # deux pages pour tester la pagination
        "/api/v4/projects/1/pipelines": [
            [{"status": "failed", "created_at": "2099-01-01T00:00:00Z"}],
            [{"status": "success", "created_at": "2099-01-01T00:00:00Z"}],
        ],
    }


class FakeGitLab(BaseHTTPRequestHandler):
    routes = _routes()

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):
        url = urlparse(self.path)
        qs = parse_qs(url.query)
        if self.headers.get("PRIVATE-TOKEN") != TOKEN:
            self.send_response(401)
            self.end_headers()
            return
        key = url.path
        if "state" in qs:
            key += "|" + qs["state"][0]
        pages = self.routes.get(key)
        if pages is None:
            self.send_response(404)
            self.end_headers()
            return
        page = int(qs.get("page", ["1"])[0])
        body = json.dumps(pages[page - 1]).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if page < len(pages):
            self.send_header("X-Next-Page", str(page + 1))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def fake_gitlab():
    server = HTTPServer(("127.0.0.1", 0), FakeGitLab)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_end_to_end_metrics_exposition(fake_gitlab):
    client = GitLabClient(fake_gitlab, TOKEN)
    snap = collect_once(client, "demo", history_days=30, workflow_prefix="workflow::")

    registry = CollectorRegistry()
    collector = SnapshotCollector()
    registry.register(collector)
    collector.update(snap, 0.1)
    text = generate_latest(registry).decode()

    assert 'gitlab_issues_open{project="demo/api"} 1.0' in text
    assert 'gitlab_issues_open_by_workflow{project="demo/api",workflow="todo"} 1.0' in text
    assert 'gitlab_issues_weight_remaining{milestone="none",project="demo/api"} 3.0' in text
    # 1 échec + 1 succès répartis sur 2 pages -> pagination vérifiée
    assert 'gitlab_pipelines_failure_ratio{project="demo/api"} 0.5' in text
    assert "gitlab_exporter_up 1.0" in text

    collector.mark_failed()
    assert "gitlab_exporter_up 0.0" in generate_latest(registry).decode()


def test_bad_token_raises(fake_gitlab):
    client = GitLabClient(fake_gitlab, "wrong")
    with pytest.raises(GitLabError):
        collect_once(client, "demo", 30, "workflow::")


def test_http_server_serves_metrics(fake_gitlab):
    from prometheus_client import start_http_server

    registry = CollectorRegistry()
    collector = SnapshotCollector()
    registry.register(collector)
    collector.update(collect_once(GitLabClient(fake_gitlab, TOKEN), "demo", 30, "workflow::"), 0.2)
    server, _thread = start_http_server(0, addr="127.0.0.1", registry=registry)
    try:
        port = server.server_port
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics") as resp:
            assert resp.status == 200
            assert b"gitlab_issues_open" in resp.read()
    finally:
        server.shutdown()
