"""`POST /extensions/gitea/webhook/<integration_id>/<repo_id>/`: signed Gitea deliveries.

Order of checks: method, bounded body, URL with a repository ID and an active Gitea
integration (404), signature over the raw bytes with that repository's derived secret (401),
JSON and payload shape, and a payload naming that same repository (400). Authenticated
deliveries for events, actions, owners or repositories we do not handle are acknowledged with
204 and change nothing. Only repositories added through Add Repository (which recorded a
hook) are processed, in every organization sharing the integration; one organization's
failure does not stop the others (503 for Gitea/API errors, else 500, after all ran).
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Any

from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from sentry.api.api_owners import ApiOwner
from sentry.api.api_publish_status import ApiPublishStatus
from sentry.api.base import Endpoint, cell_silo_endpoint
from sentry.constants import ObjectStatus
from sentry.integrations.services.integration import integration_service
from sentry.integrations.source_code_management.webhook import SCMWebhook
from sentry.integrations.utils.metrics import IntegrationWebhookEventType
from sentry.models.repository import Repository
from sentry.shared_integrations.exceptions import ApiError, IntegrationError

from sentry_gitea.api.webhooks import (
    ASSIGN_ACTIONS,
    EVENTS,
    ISSUE_EVENTS,
    MAX_PAYLOAD_BYTES,
    STATUS_ACTIONS,
    IssueEvent,
    PayloadError,
    PullRequestEvent,
    PushEvent,
    parse_issue,
    parse_json,
    parse_pull_request,
    parse_push,
    verify_signature,
)

from .blame import mark_mirror_stale
from .commits import store_commits, upsert_pull_request
from .identity import log_failure
from .repository import PROVIDER, SYNC_FAILED
from .source import repository_url
from .tasks import backfill_push
from .utils import issue_key, repo_webhook_secret

logger = logging.getLogger(__name__)


class GiteaWebhook(SCMWebhook):
    provider = "gitea"
    event_type = IntegrationWebhookEventType.INBOUND_SYNC

    def update_repo_data(self, repo: Any, event: Any) -> None:
        """Follow renames of the stable repository ID; the delivery is already authenticated."""
        ref = event.repository
        config = dict(repo.config or {})
        changes: dict[str, Any] = {}
        if repo.name != ref.full_name or config.get("path") != ref.full_name:
            config["path"] = ref.full_name
            changes.update(name=ref.full_name, url=repository_url(ref.full_name))
        if ref.default_branch and config.get("default_branch") != ref.default_branch:
            config["default_branch"] = ref.default_branch
        if changes or config != (repo.config or {}):
            repo.update(config=config, **changes)

    def __call__(self, event: Any, **kwargs: Any) -> None:
        raise NotImplementedError


class PushWebhook(GiteaWebhook):
    event_type = IntegrationWebhookEventType.PUSH

    def __call__(self, event: PushEvent, **kwargs: Any) -> None:
        if event.deleted:
            return  # Branch deletion: nothing to import.
        repo = kwargs["repo"]
        store_commits(repo, event.commits)
        mark_mirror_stale(repo)
        # Branch creation imports only the delivered commits; no history walk.
        if event.truncated and not event.created:
            backfill_push.delay(repo.id, event.before, event.after)


class PullRequestWebhook(GiteaWebhook):
    event_type = IntegrationWebhookEventType.MERGE_REQUEST

    def __call__(self, event: PullRequestEvent, **kwargs: Any) -> None:
        upsert_pull_request(kwargs["repo"], event)


class IssueWebhook(GiteaWebhook):
    event_type = IntegrationWebhookEventType.INBOUND_SYNC

    def __call__(self, event: IssueEvent, **kwargs: Any) -> None:
        installation = kwargs["integration"].get_installation(
            organization_id=kwargs["repo"].organization_id
        )
        key = issue_key(event.repository.id, event.number)
        if event.action in STATUS_ACTIONS:
            # Queues Sentry's inbound status task, which re-reads the issue in Gitea.
            installation.sync_status_inbound(key, {"issue_key": key, "action": event.action})
        elif event.action in ASSIGN_ACTIONS:
            installation.sync_assignee_inbound(key)


HANDLERS: dict[str, type[GiteaWebhook]] = {
    "push": PushWebhook,
    "pull_request": PullRequestWebhook,
    "issues": IssueWebhook,
    "issue_assign": IssueWebhook,
}


@cell_silo_endpoint
class GiteaWebhookEndpoint(Endpoint):
    owner = ApiOwner.CODING_WORKFLOWS
    publish_status = {"POST": ApiPublishStatus.PRIVATE}
    authentication_classes = ()
    permission_classes = ()

    @method_decorator(csrf_exempt)
    def dispatch(self, request: Any, *args: Any, **kwargs: Any) -> Any:
        if request.method != "POST":
            return HttpResponse(status=405)
        # Read at most one byte past the limit before anything else touches the body.
        length = request.META.get("CONTENT_LENGTH") or ""
        if length and (not length.isdigit() or int(length) > MAX_PAYLOAD_BYTES):
            return self._rejected(kwargs.get("integration_id"), "payload", 400)
        body = request.read(MAX_PAYLOAD_BYTES + 1)
        if len(body) > MAX_PAYLOAD_BYTES:
            return self._rejected(kwargs.get("integration_id"), "payload", 400)
        request._body = body
        request._stream = BytesIO(body)
        return super().dispatch(request, *args, **kwargs)

    @staticmethod
    def _rejected(integration_id: Any, reason: str, status: int) -> HttpResponse:
        # Never log the body, the signature or the secret.
        logger.warning(
            "sentry-gitea: webhook rejected",
            extra={"integration_id": integration_id, "reason": reason, "status": status},
        )
        return HttpResponse(status=status)

    @staticmethod
    def _ignored(integration_id: Any, reason: str, event: str) -> HttpResponse:
        logger.info(
            "sentry-gitea: webhook ignored",
            extra={"integration_id": integration_id, "reason": reason, "event": event[:64]},
        )
        return HttpResponse(status=204)

    @staticmethod
    def _signing_key(integration: Any, repo_id: str) -> str | None:
        try:
            return repo_webhook_secret(integration.metadata.get("webhook_secret"), repo_id)
        except ValueError:
            return None  # No integration secret: nothing can authenticate.

    def post(self, request: Any, integration_id: str, repo_id: str | None = None) -> HttpResponse:
        if repo_id is None or len(repo_id) > 18 or not repo_id.isdigit() or repo_id[0] == "0":
            # Includes hooks created before per-repository secrets: add the repository again.
            return self._rejected(integration_id, "url", 404)
        integration = (
            integration_service.get_integration(integration_id=int(integration_id))
            if len(integration_id) <= 18
            else None
        )
        if (
            integration is None
            or integration.provider != "gitea"
            or integration.status != ObjectStatus.ACTIVE
        ):
            return self._rejected(integration_id, "integration", 404)
        body = request.body
        if not verify_signature(
            body,
            request.META.get("HTTP_X_GITEA_SIGNATURE"),
            self._signing_key(integration, repo_id),
        ):
            return self._rejected(integration.id, "signature", 401)
        event_name = str(request.META.get("HTTP_X_GITEA_EVENT") or "")
        try:
            payload = parse_json(body)
            if event_name not in EVENTS:
                return self._ignored(integration.id, "event", event_name)
            if event_name == "push":
                event: Any = parse_push(payload)
            elif event_name == "pull_request":
                event = parse_pull_request(payload)
            else:
                event = parse_issue(payload)
        except PayloadError:
            return self._rejected(integration.id, "payload", 400)
        if str(event.repository.id) != repo_id:
            # A repository's secret signs deliveries for that repository only.
            return self._rejected(integration.id, "repository", 400)
        installs = integration_service.get_organization_integrations(
            integration_id=integration.id, status=ObjectStatus.ACTIVE
        )
        if not installs:
            return self._rejected(integration.id, "integration", 404)
        if event_name in ISSUE_EVENTS and (
            event.is_pull_request or event.action not in STATUS_ACTIONS | ASSIGN_ACTIONS
        ):
            return self._ignored(integration.id, "action", event_name)
        if str(event.repository.owner_id) != str(integration.metadata.get("owner_id")):
            # Signed by our hook, but the repository left the installed owner (transfer).
            return self._ignored(integration.id, "owner", event_name)
        repos = [
            repo
            for repo in Repository.objects.filter(
                organization_id__in=[install.organization_id for install in installs],
                integration_id=integration.id,
                provider=PROVIDER,
                external_id=str(event.repository.id),
                status=ObjectStatus.ACTIVE,
            ).order_by("id")
            if isinstance(repo.config, dict) and repo.config.get("hook_id")
        ]
        if not repos:
            return self._ignored(integration.id, "repository", event_name)
        handler = HANDLERS[event_name]()
        failures: set[int] = set()
        for repo in repos:
            try:
                handler.update_repo_data(repo, event)
                handler(event, repo=repo, integration=integration, event_name=event_name)
            except Exception as error:
                # One organization's failure must not stop the others. Logged without payload
                # data or traceback locals; a redelivery is safe (handlers are idempotent).
                log_failure(
                    logger,
                    SYNC_FAILED,
                    error,
                    operation=f"webhook_{event_name}",
                    integration_id=integration.id,
                    organization_id=repo.organization_id,
                    repository_id=repo.id,
                )
                failures.add(503 if isinstance(error, (ApiError, IntegrationError)) else 500)
        # 503: only Gitea/API errors (a redelivery may succeed); 500: an unexpected failure.
        return HttpResponse(status=500 if 500 in failures else 503 if failures else 204)
