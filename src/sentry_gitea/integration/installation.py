"""Owner-scoped issue tracking, synchronization and source repositories."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, NoReturn

from sentry.integrations.source_code_management.commit_context import (
    CommitContextIntegration,
    FileBlameInfo,
    SourceLineInfo,
)
from sentry.shared_integrations.exceptions import ApiError, IntegrationError
from sentry.users.models.identity import Identity

from sentry_gitea.api.errors import GiteaApiError

from .blame import blame_for_files
from .client import GiteaIntegrationClient, api_error
from .issue_sync import GiteaIssueSync
from .issues import GiteaIssues
from .source import GiteaSource


class GiteaIntegration(GiteaIssueSync, GiteaIssues, GiteaSource, CommitContextIntegration):
    logger = logging.getLogger(__name__)

    @property
    def integration_id(self) -> int:
        return self.model.id

    def get_blame_for_files(
        self, files: Sequence[SourceLineInfo], extra: dict[str, Any]
    ) -> list[FileBlameInfo]:
        return blame_for_files(self, files)

    def on_create_or_update_comment_error(self, api_error: ApiError, metrics_base: str) -> bool:
        # Without get_pr_comment_workflow(), Sentry never queues pull-request comments.
        raise NotImplementedError("Gitea has no pull-request comment bot.")

    def get_client(self) -> GiteaIntegrationClient:
        try:
            identity = self.default_identity
        except Identity.DoesNotExist:
            raise IntegrationError(
                "Gitea identity is missing. Reinstall the integration."
            ) from None
        return GiteaIntegrationClient(self, identity)

    def get_repositories(
        self,
        query: str | None = None,
        page_number_limit: int | None = None,
        accessible_only: bool = False,
        use_cache: bool = False,
        raise_on_page_limit: bool = False,
        parallel: bool = False,
    ) -> list[dict[str, Any]]:
        with self._issue_operation():
            rows = self.get_client().search_repositories(query, page_number_limit, issues=False)
            return [
                {
                    "identifier": row["full_name"],
                    "name": row["full_name"],
                    "external_id": str(row["id"]),
                    "default_branch": row.get("default_branch") or "main",
                }
                for row in rows
            ]

    def _get_debug_metadata_keys(self) -> list[str]:
        return ["base_url", "owner_kind", "owner_id", "owner_login"]

    def error_message_from_json(self, data: Mapping[str, Any]) -> str:
        return str(data.get("message") or "Gitea could not complete the request.")[:300]

    def error_fields_from_json(self, data: Mapping[str, Any]) -> None:
        # Gitea's response bodies are untrusted; field validation is performed locally.
        return None

    def message_from_error(self, exc: Exception) -> str:
        if isinstance(exc, GiteaApiError):
            exc = api_error(exc)
        if isinstance(exc, ApiError):
            if exc.code == 401:
                return "Gitea authorization could not be renewed. Reinstall the integration."
            return f"Gitea (HTTP {exc.code}): {self.error_message_from_json(exc.json or {})}"
        return "Gitea could not complete the operation. Check the fields and retry."

    def raise_error(self, exc: Exception, identity: Any = None) -> NoReturn:
        if isinstance(exc, IntegrationError):
            raise exc
        if isinstance(exc, GiteaApiError):
            exc = api_error(exc)
        raise IntegrationError(self.message_from_error(exc)) from None
