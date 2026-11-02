"""Taskworker tasks: push backfill and the OAuth keep-alive.

Registered through `TASKWORKER_IMPORTS += ("sentry_gitea.integration.tasks",)` in the existing
`integrations` taskworker namespace.

- `backfill_push` recovers commits that Gitea truncated from a push payload. Commits are
  upserted page by page, so a retry or a replayed delivery only repeats idempotent writes.
- `mirror_repository` clones the bare git mirror that blame-based suspect commits read
  (GITEA_BLAME_ENABLED). Creating an existing mirror is a no-op.
- `refresh_identities` keeps idle installations authorized: Gitea refresh tokens expire
  (`REFRESH_TOKEN_EXPIRATION_TIME`, 730 h by default) unless used. Operators schedule it with
  `TASKWORKER_SCHEDULES["gitea-identity-refresh"] = {"task":
  "integrations:sentry_gitea.integration.refresh_identities", "schedule": timedelta(hours=12)}`.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sentry.constants import ObjectStatus
from sentry.identity.services.identity import identity_service
from sentry.integrations.models.integration import Integration
from sentry.integrations.services.integration import integration_service
from sentry.models.repository import Repository
from sentry.shared_integrations.exceptions import ApiError, ApiUnauthorized, IntegrationError
from sentry.silo.base import SiloMode
from sentry.tasks.base import instrumented_task
from sentry.taskworker.namespaces import integrations_tasks

from sentry_gitea.api.client import is_sha
from sentry_gitea.api.webhooks import PayloadError, is_zero_sha, normalize_api_commit
from sentry_gitea.mirror import CLONE_TIMEOUT, GitError

from .blame import BLAME_FAILED, clone_mirror
from .commits import store_commits
from .identity import ConfigurationError, GiteaIdentityProvider, OAuthSettings, log_failure
from .repository import PROVIDER, SYNC_FAILED, GiteaRepositoryProvider

logger = logging.getLogger(__name__)
BACKFILL_LIMIT = 1000
# Three retries for transient Gitea failures, with growing delays (seconds).
RETRY_DELAYS = (60, 300, 900)
BACKFILL_BUDGET = 240
# Identities whose tokens Gitea issued at least this long ago are refreshed by the keep-alive.
KEEPALIVE_AGE = 7 * 24 * 3600
KEEPALIVE_BUDGET = 240


def transient(error: Exception) -> bool:
    code = getattr(error, "code", None)
    return isinstance(error, ApiError) and (code in {None, 0, 408, 429} or (code or 0) >= 500)


@instrumented_task(
    name="sentry_gitea.integration.tasks.backfill_push",
    namespace=integrations_tasks,
    processing_deadline_duration=BACKFILL_BUDGET + 60,
    silo_mode=SiloMode.CELL,
)
def backfill_push(repository_id: int, before: str, after: str, attempt: int = 0) -> None:
    """Import commits reachable from `after` but not `before`, newest first, at most 1,000."""
    if not (is_sha(before) and is_sha(after)) or is_zero_sha(before) or is_zero_sha(after):
        return
    repo = Repository.objects.filter(
        id=repository_id, provider=PROVIDER, status=ObjectStatus.ACTIVE
    ).first()
    if repo is None or repo.integration_id is None or not (repo.config or {}).get("hook_id"):
        return
    imported = 0

    def store_page(rows: list[dict[str, Any]]) -> None:
        nonlocal imported
        commits = [normalize_api_commit(row, repo.name) for row in rows]
        imported += store_commits(repo, [commit for commit in commits if commit is not None])

    try:
        installation = GiteaRepositoryProvider(PROVIDER).get_installation(
            repo.integration_id, repo.organization_id
        )
        client = installation.get_client()
        client.failure_message = SYNC_FAILED
        client.extend_deadline(BACKFILL_BUDGET)
        result = client.commit_range(
            client.repository_path(repo),
            after,
            exclude=before,
            max_commits=BACKFILL_LIMIT,
            on_page=store_page,
        )
    except (ApiError, IntegrationError, PayloadError, Integration.DoesNotExist) as error:
        retry = transient(error) and attempt < len(RETRY_DELAYS)
        log_failure(
            logger,
            SYNC_FAILED,
            error,
            operation="backfill_push",
            repository_id=repository_id,
            attempt=attempt,
            retrying=retry,
            imported_count=imported,
        )
        if retry:
            backfill_push.apply_async(
                args=[repository_id, before, after],
                kwargs={"attempt": attempt + 1},
                countdown=RETRY_DELAYS[attempt],
            )
        return
    if not result.complete:
        logger.warning(
            "sentry-gitea: backfill incomplete",
            extra={
                "repository_id": repository_id,
                "limit": BACKFILL_LIMIT,
                "fetched_count": result.fetched,
                "total_count": result.total,
                "imported_count": imported,
            },
        )


def keepalive_identity_ids() -> list[int]:
    """Default identities of active Gitea installations in active organizations."""
    active = {
        integration.id
        for integration in integration_service.get_integrations(
            providers=["gitea"], status=ObjectStatus.ACTIVE
        )
    }
    if not active:
        return []
    return sorted(
        {
            install.default_auth_id
            for install in integration_service.get_organization_integrations(
                providers=["gitea"], status=ObjectStatus.ACTIVE
            )
            if install.integration_id in active and install.default_auth_id is not None
        }
    )


@instrumented_task(
    name="sentry_gitea.integration.refresh_identities",
    namespace=integrations_tasks,
    # One refresh may wait 45 s for its lock plus the OAuth request after the loop budget.
    processing_deadline_duration=KEEPALIVE_BUDGET + 120,
    silo_mode=SiloMode.CELL,
)
def refresh_identities() -> None:
    """Refresh installation identities whose tokens were issued at least seven days ago.

    Identities from before this task existed carry no `issued_at` and are refreshed once. A
    failure (for example a refresh token that already expired: the installation must be
    reinstalled) is logged per identity and does not stop the others.
    """
    started, now = time.monotonic(), time.time()
    wanted = set(keepalive_identity_ids())
    if not wanted:
        return
    try:
        OAuthSettings.load()
    except ConfigurationError as error:
        log_failure(logger, "sentry-gitea: token refresh failed", error, reason="configuration")
        return
    due = [
        identity
        for identity in identity_service.get_identities(filter={"provider_type": "gitea"})
        if identity.id in wanted
        and identity.data.get("refresh_token")
        and not (
            type(identity.data.get("issued_at")) is int
            and now - identity.data["issued_at"] < KEEPALIVE_AGE
        )
    ]
    refreshed = failed = 0
    for identity in due:
        if time.monotonic() - started > KEEPALIVE_BUDGET:
            break  # The rest stay due for the next scheduled run.
        try:
            GiteaIdentityProvider().refresh_identity(
                identity, failed_access_token=identity.data.get("access_token")
            )
        except ApiUnauthorized:
            failed += 1  # Logged by refresh_identity, with the identity ID.
        else:
            refreshed += 1
    logger.info(
        "sentry-gitea: identity keep-alive",
        extra={
            "due_count": len(due),
            "refreshed_count": refreshed,
            "failed_count": failed,
            "deferred_count": len(due) - refreshed - failed,
        },
    )


@instrumented_task(
    name="sentry_gitea.integration.mirror_repository",
    namespace=integrations_tasks,
    processing_deadline_duration=CLONE_TIMEOUT + 120,
    silo_mode=SiloMode.CELL,
)
def mirror_repository(repository_id: int) -> None:
    repo = Repository.objects.filter(
        id=repository_id, provider=PROVIDER, status=ObjectStatus.ACTIVE
    ).first()
    if repo is None or repo.integration_id is None:
        return
    try:
        installation = GiteaRepositoryProvider(PROVIDER).get_installation(
            repo.integration_id, repo.organization_id
        )
        clone_mirror(installation, repo)
    except (GitError, ApiError, IntegrationError) as error:
        log_failure(logger, BLAME_FAILED, error, operation="mirror", repository_id=repository_id)
