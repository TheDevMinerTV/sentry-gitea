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
from sentry.shared_integrations.exceptions import (
    ApiError,
    IntegrationError,
    IntegrationFormError,
)
from sentry.users.models.identity import Identity

from sentry_gitea.api.errors import GiteaApiError

from .blame import blame_enabled, blame_for_files
from .client import GiteaIntegrationClient, api_error
from .issue_sync import GiteaIssueSync
from .issues import GiteaIssues
from .repository import remove_unselected_repositories
from .selection import (
    REPOSITORIES_KEY,
    is_selected,
    parse_repository_ids,
    repository_choices,
    stored_selection,
)
from .source import GiteaSource


class GiteaIntegration(GiteaIssueSync, GiteaIssues, GiteaSource, CommitContextIntegration):
    logger = logging.getLogger(__name__)

    @property
    def integration_id(self) -> int:
        return self.model.id

    def get_blame_for_files(
        self, files: Sequence[SourceLineInfo], extra: dict[str, Any]
    ) -> list[FileBlameInfo]:
        # Sentry blames every code mapping of a project once one listed provider qualifies, and
        # checks each integration only by type, so this class is asked even with blame off.
        if not blame_enabled():
            return []
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
        selection = self._selection()
        return [
            {
                "identifier": row["full_name"],
                "name": row["full_name"],
                "external_id": str(row["id"]),
                "default_branch": row.get("default_branch") or "main",
            }
            for row in rows
            if is_selected(selection, row["id"])
        ]

    def _selection(self) -> frozenset[int] | None:
        return stored_selection(self.org_integration.config if self.org_integration else None)

    def get_organization_config(self) -> list[dict[str, Any]]:
        selection = self._selection()
        field: dict[str, Any] = {
            "name": REPOSITORIES_KEY,
            "type": "select",
            "multiple": True,
            "label": "Repositories",
            "help": (
                "Repository sync imports only these repositories and installs their webhooks. "
                "Removing one deletes its Sentry repository, with its commits and code mappings, "
                "its webhook and its blame mirror."
            ),
        }
        try:
            with self._issue_operation():
                rows = self.get_client().search_repositories(issues=False)
            choices = repository_choices(rows, self.model.metadata["owner_id"])
            field["choices"] = [[choice.id, choice.full_name] for choice in choices]
        except IntegrationError:
            # Saving a list built without Gitea's answer could delete every repository.
            field["choices"] = [
                [repo_id, f"Repository {repo_id}"] for repo_id in sorted(selection or ())
            ]
            field["disabled"] = True
            field["disabledReason"] = "Gitea could not list the repositories right now."
        if selection is None:
            field["default"] = [repo_id for repo_id, _ in field["choices"]]
        return [field, *super().get_organization_config()]

    def update_organization_config(self, data: Any) -> None:
        data = dict(data)
        if REPOSITORIES_KEY in data:
            try:
                data[REPOSITORIES_KEY] = parse_repository_ids(data[REPOSITORIES_KEY])
            except ValueError as error:
                raise IntegrationFormError({REPOSITORIES_KEY: [str(error)]}) from None
        super().update_organization_config(data)
        if REPOSITORIES_KEY in data and self.org_integration is not None:
            remove_unselected_repositories(self)
            from sentry.integrations.source_code_management.sync_repos import sync_repos_for_org

            sync_repos_for_org.apply_async(
                kwargs={"organization_integration_id": self.org_integration.id}
            )

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
