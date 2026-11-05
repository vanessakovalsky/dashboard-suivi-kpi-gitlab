"""Client minimal pour l'API REST v4 de GitLab (bibliothèque standard uniquement)."""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Any


class GitLabError(RuntimeError):
    """Erreur d'appel à l'API GitLab."""


class GitLabClient:
    def __init__(
        self, base_url: str, token: str, timeout: float = 30.0, max_pages: int = 50
    ) -> None:
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("GITLAB_URL doit commencer par http:// ou https://")
        self.base_url = base_url.rstrip("/") + "/api/v4"
        self.token = token
        self.timeout = timeout
        self.max_pages = max_pages

    def _get(self, path: str, params: dict[str, Any]) -> tuple[Any, dict[str, str]]:
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}?{query}"
        # Schéma http(s) vérifié dans __init__ : pas de file:// ni schéma custom.
        headers_out = {"PRIVATE-TOKEN": self.token, "Accept": "application/json"}
        req = urllib.request.Request(url, headers=headers_out)  # noqa: S310
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310  # nosec B310
                body = json.loads(resp.read().decode("utf-8"))
                headers = {k.lower(): v for k, v in resp.headers.items()}
        except Exception as exc:  # réseau, HTTP, JSON
            raise GitLabError(f"GET {path} : {exc}") from exc
        return body, headers

    def paginate(self, path: str, **params: Any) -> list[dict[str, Any]]:
        """Récupère toutes les pages (pagination par offset, X-Next-Page)."""
        items: list[dict[str, Any]] = []
        page = 1
        while page and page <= self.max_pages:
            body, headers = self._get(path, {**params, "per_page": 100, "page": page})
            if not isinstance(body, list):
                raise GitLabError(f"Réponse inattendue pour {path}")
            items.extend(body)
            next_page = headers.get("x-next-page", "")
            page = int(next_page) if next_page.isdigit() else 0
        return items

    def group_path(self, group: str) -> str:
        return "/groups/" + urllib.parse.quote(group, safe="")
