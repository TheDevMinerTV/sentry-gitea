"""Gitea owner installations, using Sentry's generic integration pipeline."""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from typing import Any

from django.http import HttpRequest, HttpResponse, HttpResponseBase
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from sentry import identity
from sentry.identity.pipeline import IdentityPipeline
from sentry.integrations.base import (
    FeatureDescription,
    IntegrationFeatures,
    IntegrationMetadata,
    IntegrationProvider,
)
from sentry.integrations.models.integration import Integration
from sentry.pipeline.views.nested import NestedPipelineView
from sentry.plugins.base import bindings
from sentry.shared_integrations.exceptions import IntegrationError

from sentry_gitea.api.client import GiteaClient, issue_number, normalize_repo
from sentry_gitea.api.errors import GiteaApiError
from sentry_gitea.meta import AUTHOR, ISSUES_URL, PROJECT_URL

from .blame import blame_enabled
from .identity import (
    ConfigurationError,
    GiteaIdentityProvider,
    OAuthSettings,
    fail_pipeline,
    identity_from_state,
    log_failure,
    setup_path,
)
from .installation import GiteaIntegration
from .pages import render_page
from .repository import GiteaRepositoryProvider
from .utils import external_id

logger = logging.getLogger(__name__)

# Provider loading is the supported out-of-tree identity registration point.
if not identity.exists("gitea"):
    identity.register(GiteaIdentityProvider)


def available_owners(client: GiteaClient) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    user = client.get_user()
    login = normalize_repo(f"{user.get('login', '')}/namespace").split("/")[0]
    owners = [{"kind": "user", "id": int(issue_number(user.get("id"))), "login": login}]
    for row in client.list_user_orgs():
        org_login = row.get("name") or row.get("username")
        if not isinstance(org_login, str):
            raise ValueError("Invalid Gitea organization.")
        normalize_repo(f"{org_login}/namespace")
        if not client.is_org_owner(org_login, login):
            continue
        org = client.get_org(org_login)
        org_id = int(issue_number(org.get("id")))
        if org_id != row.get("id"):
            raise ValueError("Gitea organization changed.")
        owners.append({"kind": "org", "id": org_id, "login": org_login})
    return user, owners


def selected_owner(owners: list[dict[str, Any]], selection: Any) -> dict[str, Any]:
    for owner in owners:
        if selection == f"{owner['kind']}:{owner['id']}":
            return owner
    raise ValueError("Select your own namespace or an organization you own.")


class OwnerSelectionView:
    @method_decorator(csrf_protect)
    def dispatch(self, request: HttpRequest, pipeline: Any) -> HttpResponseBase:
        try:
            config = OAuthSettings.load()
            state = pipeline.fetch_state("identity")
            with GiteaClient(
                config.base_url, state["tokens"]["access_token"], token_type="Bearer"
            ) as client:
                user, owners = available_owners(client)
                identity_from_state(config.base_url, user, state)
            if request.method == "POST":
                owner = selected_owner(owners, request.POST.get("owner"))
                pipeline.bind_state("owner", f"{owner['kind']}:{owner['id']}")
                return pipeline.next_step()
            if request.method != "GET":
                return HttpResponse(status=405)
            response = render_page(
                request, "owner-choice.html", {"action": setup_path(), "owners": owners}
            )
            # The callback URL contains code/state. Sentry's CSRF failure view logs Referer;
            # retain its origin for HTTPS CSRF checks without forwarding the callback query.
            response["Referrer-Policy"] = "origin"
            return response
        except (ConfigurationError, GiteaApiError, ValueError, KeyError, TypeError) as error:
            log_failure(logger, "sentry-gitea: install failed", error)
            return fail_pipeline(
                pipeline,
                "Unable to verify the Gitea owner. Start setup again with an owner account.",
            )


class GiteaIntegrationProvider(IntegrationProvider):
    key = "gitea"
    name = "Gitea"
    # Sentry 26.10's install modal only renders its built-in providers, so the directory
    # links to this package's own install page instead.
    can_add = False
    allow_multiple = True
    can_disable = False
    needs_default_identity = True
    requires_feature_flag = False
    integration_cls = GiteaIntegration
    features = frozenset(
        {
            IntegrationFeatures.ISSUE_BASIC,
            IntegrationFeatures.ISSUE_SYNC,
            IntegrationFeatures.COMMITS,
            IntegrationFeatures.STACKTRACE_LINK,
            IntegrationFeatures.CODEOWNERS,
        }
    )
    metadata = IntegrationMetadata(
        description=(
            "Create, link and sync Gitea issues, import commits and pull requests, and link "
            "stack traces and CODEOWNERS, using an owner-scoped OAuth installation."
        ),
        features=(
            FeatureDescription(
                "Create, search and link Gitea issues.", IntegrationFeatures.ISSUE_BASIC
            ),
            FeatureDescription(
                "Sync issue status, assignments and Sentry notes.", IntegrationFeatures.ISSUE_SYNC
            ),
            FeatureDescription(
                "Import repositories, commits and pull requests.", IntegrationFeatures.COMMITS
            ),
            FeatureDescription(
                "Link stack frames through code mappings.", IntegrationFeatures.STACKTRACE_LINK
            ),
            FeatureDescription(
                "Import CODEOWNERS with email owners.", IntegrationFeatures.CODEOWNERS
            ),
        ),
        author=AUTHOR,
        noun="Gitea owner",
        issue_url=ISSUES_URL,
        source_url=PROJECT_URL,
        aspects={
            "externalInstall": {
                "url": "/extensions/gitea/install/",
                "buttonText": "Install",
                "noticeText": (
                    "Install opens a separate page: authorize in Gitea and choose an owner. "
                    "Reload this page afterwards."
                ),
            }
        },
    )

    def setup(self) -> None:
        # Called by Sentry's initializer in web and worker processes; re-adding is idempotent.
        bindings.add(
            "integration-repository.provider", GiteaRepositoryProvider, id="integrations:gitea"
        )
        # Sentry 26.10's Repositories page has no Add Repository form: it only lists what the
        # repository sync (its "Sync now" button and a daily beat) imports for listed providers.
        from sentry.integrations.source_code_management import sync_repos

        if self.key not in sync_repos.SCM_SYNC_PROVIDERS:
            sync_repos.SCM_SYNC_PROVIDERS.append(self.key)
        if blame_enabled():
            # Sentry only runs blame for providers in this list. Gitea stays out unless enabled:
            # listed providers with a code mapping lose the release-based suspect commits.
            from sentry.tasks import post_process

            if self.key not in post_process.COMMIT_CONTEXT_INTEGRATION_PROVIDERS:
                post_process.COMMIT_CONTEXT_INTEGRATION_PROVIDERS.append(self.key)

    def get_pipeline_views(self) -> list[Any]:
        return [
            NestedPipelineView(
                bind_key="identity",
                pipeline_cls=IdentityPipeline,
                provider_key="gitea",
                config={"redirect_url": setup_path()},
            ),
            OwnerSelectionView(),
        ]

    def build_integration(self, state: Mapping[str, Any]) -> dict[str, Any]:
        try:
            config = OAuthSettings.load()
            auth = state["identity"]
            with GiteaClient(
                config.base_url, auth["tokens"]["access_token"], token_type="Bearer"
            ) as client:
                user, owners = available_owners(client)
            owner = selected_owner(owners, state.get("owner"))
            user_identity = identity_from_state(config.base_url, user, auth)
            identifier = external_id(config.base_url, owner["kind"], owner["id"])
            previous = (
                Integration.objects.filter(provider=self.key, external_id=identifier)
                .values_list("metadata", flat=True)
                .first()
                or {}
            )
            return {
                "external_id": identifier,
                "name": owner["login"],
                "idp_external_id": identifier,
                "idp_config": {},
                "user_identity": user_identity,
                "metadata": {
                    "base_url": config.base_url,
                    "public_url": config.public_url,
                    "owner_kind": owner["kind"],
                    "owner_id": owner["id"],
                    "owner_login": owner["login"],
                    "scopes": user_identity["scopes"],
                    "webhook_secret": previous.get("webhook_secret") or secrets.token_hex(32),
                },
            }
        except (ConfigurationError, GiteaApiError, ValueError, KeyError, TypeError) as error:
            log_failure(logger, "sentry-gitea: install failed", error)
            raise IntegrationError(
                "Unable to verify the Gitea owner. Start setup again with an owner account."
            ) from None
