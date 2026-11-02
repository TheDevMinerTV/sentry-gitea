"""Repository registration, the signed Gitea hook lifecycle and release commit ranges."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from django.conf import settings
from django.db import router, transaction
from django.db.models import Q
from sentry import options
from sentry.constants import ObjectStatus
from sentry.integrations.models.integration import Integration
from sentry.locks import locks
from sentry.models.repository import Repository
from sentry.plugins.providers.integration_repository import (
    IntegrationRepositoryProvider,
    RepositoryConfig,
)
from sentry.shared_integrations.exceptions import ApiError, IntegrationError
from sentry.utils.locking import UnableToAcquireLock

from sentry_gitea.api.client import git_ref, issue_number, normalize_base_url, normalize_repo
from sentry_gitea.api.webhooks import PayloadError, normalize_api_commit
from sentry_gitea.mirror import GitError

from .blame import BLAME_FAILED, blame_enabled, repository_mirror, stable_repo_id
from .identity import log_failure
from .source import repository_link_path, repository_url
from .utils import repo_webhook_secret

logger = logging.getLogger(__name__)
PROVIDER = "integrations:gitea"
SYNC_FAILED = "sentry-gitea: sync failed"
RELEASE_LIMIT = 5000
RELEASE_DEFAULT_COUNT = 20
RELEASE_BUDGET = 600  # fetch_commits allows 15 minutes per activation
GONE = (ObjectStatus.PENDING_DELETION, ObjectStatus.DELETION_IN_PROGRESS)


def webhook_prefix() -> str:
    prefix = getattr(settings, "GITEA_WEBHOOK_URL_PREFIX", None) or options.get("system.url-prefix")
    try:
        return normalize_base_url(prefix)
    except ValueError:
        raise IntegrationError(
            "Set GITEA_WEBHOOK_URL_PREFIX (or system.url-prefix) to a URL Gitea can reach."
        ) from None


def webhook_url(integration_id: int, repo_id: int | str) -> str:
    """`<prefix>/extensions/gitea/webhook/<integration id>/<repository id>/`."""
    return (
        f"{webhook_prefix()}/extensions/gitea/webhook/{int(integration_id)}/"
        f"{issue_number(repo_id)}/"
    )


def recorded_hook_ids(integration_id: int, repo_id: str, *, exclude_id: int | None = None) -> set:
    """Hook IDs that Repository rows of this integration (any organization) still rely on."""
    rows = Repository.objects.filter(
        integration_id=integration_id, provider=PROVIDER, external_id=str(repo_id)
    ).exclude(status__in=GONE)
    if exclude_id is not None:
        rows = rows.exclude(id=exclude_id)
    return {
        str(config.get("hook_id"))
        for config in rows.values_list("config", flat=True)
        if isinstance(config, dict) and config.get("hook_id")
    }


class GiteaRepositoryProvider(IntegrationRepositoryProvider):
    name = "Gitea"
    repo_provider = "gitea"

    def get_installation(self, integration_id: int | None, organization_id: int) -> Any:
        installation = super().get_installation(integration_id, organization_id)
        org_integration = installation.org_integration
        if (
            installation.model.provider != "gitea"
            or installation.model.status != ObjectStatus.ACTIVE
            or org_integration is None
            or org_integration.status != ObjectStatus.ACTIVE
        ):
            raise IntegrationError("Select an active Gitea installation in this organization.")
        return installation

    @contextmanager
    def _operation(self, installation: Any, operation: str) -> Iterator[Any]:
        """A client logging `sentry-gitea: sync failed`; API errors become readable errors."""
        client = installation.get_client()
        client.failure_message = SYNC_FAILED
        try:
            yield client
        except (ApiError, UnableToAcquireLock) as error:
            log_failure(
                logger,
                SYNC_FAILED,
                error,
                operation=operation,
                integration_id=installation.model.id,
                organization_id=installation.organization_id,
            )
            installation.raise_error(error)

    def _lock(self, integration_id: int, repo_id: Any) -> Any:
        key = f"sentry-gitea:hook:{int(integration_id)}:{issue_number(repo_id)}"
        return locks.get(key, duration=120, name="gitea_hook").blocking_acquire(
            initial_delay=0.1, timeout=10
        )

    # Add Repository: POST /organizations/{org}/repos/ (provider=integrations:gitea).

    def get_repository_data(self, organization: Any, config: Any) -> dict[str, Any]:
        try:
            installation_id = int(issue_number(config.get("installation")))
            identifier = normalize_repo(config.get("identifier"))
        except (ValueError, TypeError):
            raise IntegrationError(
                "Select a Gitea installation and an owner/name repository."
            ) from None
        installation = self.get_installation(installation_id, organization.id)
        with self._operation(installation, "get_repository") as client:
            data = client.get_scm_repo(path=identifier)
        permissions = data.get("permissions")
        if not isinstance(permissions, dict) or permissions.get("admin") is not True:
            raise IntegrationError(
                "Gitea repository admin permission is required to install the Sentry webhook."
            )
        return {
            "installation": installation_id,
            "identifier": data["full_name"],
            "repository": data,
        }

    def _ensure_hook(self, installation: Any, client: Any, remote: dict[str, Any]) -> int:
        """Keep this integration's recorded hook only if it already uses the per-repository URL.

        Gitea never returns a hook's secret and cannot change it, so a recorded hook with any
        other URL (an older format or prefix) and any unrecorded hook at our URLs, whose secret
        is unknown, is deleted, and a new hook is created with the per-repository secret.
        """
        secret = installation.model.metadata.get("webhook_secret")
        if not isinstance(secret, str) or not secret:
            raise IntegrationError("Reinstall the Gitea integration to create a webhook secret.")
        path, repo_id = remote["full_name"], str(remote["id"])
        url = webhook_url(installation.model.id, repo_id)
        ours = {url, f"{webhook_prefix()}/extensions/gitea/webhook/{installation.model.id}/"}
        signing_key = repo_webhook_secret(secret, repo_id)
        recorded = recorded_hook_ids(installation.model.id, repo_id)
        hooks = client.list_hooks(path)

        def hook_url(hook: dict[str, Any]) -> Any:
            config = hook.get("config")
            return config.get("url") if isinstance(config, dict) else None

        reusable = next(
            (
                h
                for h in hooks
                if str(h.get("id")) in recorded and h.get("type") == "gitea" and hook_url(h) == url
            ),
            None,
        )
        for hook in hooks:
            if hook is not reusable and (str(hook.get("id")) in recorded or hook_url(hook) in ours):
                client.delete_hook(path, int(issue_number(hook.get("id"))))
        if reusable is not None:
            hook_id = int(issue_number(reusable.get("id")))
            # Repairs events and activation; the secret it was created with stays.
            client.update_hook(path, hook_id, url, signing_key)
            return hook_id
        created = client.create_hook(path, url, signing_key)
        try:
            return int(issue_number(created.get("id")))
        except ValueError:
            raise IntegrationError("Gitea returned an invalid webhook.") from None

    @staticmethod
    def _record_hook(integration_id: int, repo_id: str, hook_id: int, *, skip: Any = None) -> None:
        """Point every row sharing this repository's hook (any organization) at `hook_id`."""
        rows = Repository.objects.filter(
            integration_id=integration_id, provider=PROVIDER, external_id=repo_id
        ).exclude(status__in=GONE)
        if skip is not None:
            rows = rows.exclude(id=skip)
        for row in rows:
            config = row.config if isinstance(row.config, dict) else {}
            if config.get("hook_id") and str(config["hook_id"]) != str(hook_id):
                row.update(config={**config, "hook_id": hook_id})

    def build_repository_config(self, organization: Any, data: dict[str, Any]) -> RepositoryConfig:
        installation = self.get_installation(data["installation"], organization.id)
        remote = data["repository"]
        with (
            self._operation(installation, "install_webhook") as client,
            self._lock(installation.model.id, remote["id"]),
        ):
            hook_id = self._ensure_hook(installation, client, remote)
            self._record_hook(installation.model.id, str(remote["id"]), hook_id)
        return self._config(installation, remote, hook_id)

    def _config(self, installation: Any, remote: dict[str, Any], hook_id: int) -> RepositoryConfig:
        path = normalize_repo(remote["full_name"])
        return {
            "name": path,
            "external_id": str(remote["id"]),
            "url": repository_url(path),
            "integration_id": installation.model.id,
            "config": {
                "repo_id": int(remote["id"]),
                "path": path,
                "hook_id": hook_id,
                "default_branch": remote.get("default_branch") or "main",
            },
        }

    def create_repository(self, repo_config: dict[str, Any], organization: Any) -> Any:
        """Create or repair this organization's row and ensure exactly one signed hook.

        Unlike the generic flow, an existing row (hidden, active, or created by bulk code
        mappings with a path-valued external_id and no config) is updated in place, so adding a
        repository again is idempotent and never deletes the hook other rows rely on.
        """
        installation = self.get_installation(repo_config["installation"], organization.id)
        remote = repo_config["repository"]
        repo_id, path = str(remote["id"]), normalize_repo(remote["full_name"])
        with (
            self._operation(installation, "add_repository") as client,
            self._lock(installation.model.id, repo_id),
        ):
            rows = list(
                Repository.objects.filter(organization_id=organization.id, provider=self.id)
                .filter(
                    Q(external_id=repo_id)
                    | Q(external_id=path, integration_id=installation.model.id)
                )
                .exclude(status__in=GONE)
                .order_by("id")
            )
            if any(
                row.integration_id not in {None, installation.model.id}
                and row.status == ObjectStatus.ACTIVE
                for row in rows
            ):
                raise IntegrationError(
                    "This repository is already linked through another Gitea installation."
                )
            existing = next(
                (r for r in rows if r.external_id == repo_id), rows[0] if rows else None
            )
            before = recorded_hook_ids(installation.model.id, repo_id)
            hook_id = self._ensure_hook(installation, client, remote)
            result = self._config(installation, remote, hook_id)
            try:
                with transaction.atomic(using=router.db_for_write(Repository)):
                    if existing is None:
                        repository = Repository.objects.create(
                            organization_id=organization.id,
                            provider=self.id,
                            status=ObjectStatus.ACTIVE,
                            **result,
                        )
                    else:
                        existing.update(
                            provider=self.id,
                            status=ObjectStatus.ACTIVE,
                            name=result["name"],
                            external_id=result["external_id"],
                            url=result["url"],
                            integration_id=result["integration_id"],
                            config={**(existing.config or {}), **result["config"]},
                        )
                        repository = existing
                    # Rows of other organizations share the hook; a recreated one replaces it.
                    self._record_hook(installation.model.id, repo_id, hook_id, skip=repository.id)
            except Exception:
                if str(hook_id) not in before:
                    # Do not leave an unrecorded hook behind for a registration that failed.
                    try:
                        client.delete_hook(path, hook_id)
                    except ApiError as error:
                        log_failure(logger, SYNC_FAILED, error, operation="cleanup_webhook")
                raise
        return result, repository

    def on_create_repository(self, repo: Any, organization: Any) -> None:
        if not blame_enabled():
            return
        from .tasks import mirror_repository

        transaction.on_commit(
            lambda: mirror_repository.delay(repository_id=repo.id),
            using=router.db_for_write(Repository),
        )

    # Repository deletion (called by Sentry's deletion task with pending-deletion field names).

    def _delete_mirror(self, repo: Any) -> None:
        repo_id = stable_repo_id(repo)
        if repo_id is None or repo.integration_id is None:
            return
        shared = (
            Repository.objects.filter(
                integration_id=repo.integration_id, provider=PROVIDER, external_id=str(repo_id)
            )
            .exclude(status__in=GONE)
            .exclude(id=repo.id)
            .exists()
        )
        if shared:
            return  # Another organization sharing this integration still blames from it.
        try:
            repository_mirror(repo.integration_id, repo_id).delete()
        except (GitError, OSError) as error:
            log_failure(
                logger, BLAME_FAILED, error, operation="delete_mirror", repository_id=repo.id
            )

    def on_delete_repository(self, repo: Any) -> None:
        self._delete_mirror(repo)
        config = repo.config if isinstance(repo.config, dict) else {}
        hook_id = config.get("hook_id")
        if not hook_id or repo.integration_id is None:
            return
        stable = str(config.get("repo_id") or "")
        if not stable.isdigit():
            return
        if str(hook_id) in recorded_hook_ids(repo.integration_id, stable, exclude_id=repo.id):
            return  # Another organization sharing this integration still uses the hook.
        try:
            installation = self.get_installation(repo.integration_id, repo.organization_id)
        except Integration.DoesNotExist:
            raise IntegrationError(
                "The Gitea installation is gone; remove the webhook in Gitea."
            ) from None
        with (
            self._operation(installation, "delete_webhook") as client,
            self._lock(repo.integration_id, stable),
        ):
            if str(hook_id) in recorded_hook_ids(repo.integration_id, stable, exclude_id=repo.id):
                return
            try:
                current = client.get_scm_repo(repo_id=int(stable))
            except ApiError as error:
                if error.code == 404:
                    return  # The repository, and with it the hook, no longer exists.
                raise
            client.delete_hook(current["full_name"], int(issue_number(hook_id)))

    # Releases.

    def compare_commits(
        self, repo: Any, start_sha: str | None, end_sha: str
    ) -> list[dict[str, Any]]:
        try:
            git_ref(end_sha)
            if start_sha:
                git_ref(start_sha)
        except ValueError:
            raise IntegrationError(
                "Release commits must be valid Gitea commit references."
            ) from None
        installation = self.get_installation(repo.integration_id, repo.organization_id)
        with self._operation(installation, "compare_commits") as client:
            client.extend_deadline(RELEASE_BUDGET)
            path = client.repository_path(repo)
            if start_sha:
                result = client.commit_range(
                    path, end_sha, exclude=start_sha, max_commits=RELEASE_LIMIT, stop_when_over=True
                )
                if not result.complete:
                    # Never report a silently truncated release as complete.
                    raise IntegrationError(
                        f"The Gitea commit range has more than {RELEASE_LIMIT:,} commits; "
                        "set a more recent previous commit for this release."
                    )
            else:
                result = client.commit_range(
                    path, end_sha, exclude=None, max_commits=RELEASE_DEFAULT_COUNT
                )
        try:
            commits = [normalize_api_commit(row, repo.name) for row in result.rows]
        except PayloadError:
            raise IntegrationError("Gitea returned an invalid commit.") from None
        return [commit for commit in commits if commit is not None]

    def pull_request_url(self, repo: Any, pull_request: Any) -> str:
        return f"{repository_url(self.repository_external_slug(repo))}/pulls/" + issue_number(
            pull_request.key
        )

    def repository_external_slug(self, repo: Any) -> str:
        return repository_link_path(repo)
