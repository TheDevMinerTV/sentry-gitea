"""Original DB/Redis-only integration fixtures and installation tests."""

import hashlib
import re
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import responses
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from sentry import identity
from sentry.api.serializers import serialize
from sentry.integrations.api.serializers.models.integration import IntegrationProviderSerializer
from sentry.integrations.base import IntegrationFeatures
from sentry.integrations.manager import default_manager
from sentry.integrations.models.integration import Integration
from sentry.integrations.models.organization_integration import OrganizationIntegration
from sentry.shared_integrations.exceptions import IntegrationError
from sentry.testutils.cases import APITestCase, IntegrationTestCase
from sentry.testutils.silo import no_silo_test
from sentry.users.models.identity import Identity

from sentry_gitea.integration.provider import GiteaIntegrationProvider, OwnerSelectionView
from sentry_gitea.integration.utils import OAUTH_SCOPES

BASE = "https://forge.example/git"
API = BASE + "/api/v1"
PUBLIC = "https://public-forge.example/git"
TOKEN = "synthetic-integration-access"
REFRESH = "synthetic-integration-refresh"
USER = {"id": 2, "login": "alice"}
OWNER = {"id": 8, "username": "team"}
REPO = {
    "id": 41,
    "full_name": "team/repo",
    "name": "repo",
    "owner": {"id": 8, "login": "team"},
    "default_branch": "main",
}
TOKENS = {"access_token": TOKEN, "refresh_token": REFRESH, "expires": 4000000000}
METADATA = {
    "base_url": BASE,
    "public_url": PUBLIC,
    "owner_kind": "org",
    "owner_id": 8,
    "owner_login": "team",
    "scopes": list(OAUTH_SCOPES),
    "webhook_secret": "ab" * 32,
}
SETTINGS = {
    "GITEA_BASE_URL": BASE,
    "GITEA_PUBLIC_URL": PUBLIC,
    "GITEA_CLIENT_ID": "synthetic-app",
    "GITEA_CLIENT_SECRET": "synthetic-app-secret",
    "ROOT_URLCONF": "sentry_gitea.urls",
}


class FakePipeline:
    def __init__(self, data=None):
        self.data = data or {}
        self.organization = SimpleNamespace(id=1)
        self.config = {}
        self.advanced = False
        self.cleared = False

    def bind_state(self, key, value):
        self.data[key] = value

    def fetch_state(self, key=None):
        return self.data if key is None else self.data.get(key)

    def next_step(self):
        self.advanced = True
        return HttpResponse("next")

    def clear_session(self):
        self.cleared = True
        self.data.clear()

    def error(self, message):
        return HttpResponse(message, status=400)


@override_settings(**SETTINGS)
class IntegrationFixture(APITestCase):
    """No event ingestion; all external HTTP must be intercepted by responses."""

    def setUp(self):
        super().setUp()
        (self.integration, self.org_integration, self.identity, self.idp) = (
            self.create_identity_integration(
                self.user,
                self.organization,
                {
                    "provider": "gitea",
                    "external_id": hashlib.sha256(f"v1|{BASE}|org|8".encode()).hexdigest(),
                    "name": "team",
                    "metadata": METADATA.copy(),
                },
                {"external_id": f"{BASE}|2", "data": TOKENS.copy()},
            )
        )
        self.installation = self.integration.get_installation(self.organization.id)
        self.login_as(self.user)

    def mock_owners(self, *, is_owner=True):
        responses.get(API + "/user", json=USER)
        responses.get(API + "/user/orgs", json=[OWNER], headers={"X-Total-Count": "1"})
        responses.get(API + "/orgs/team", json=OWNER)
        responses.get(API + "/orgs/team/members/alice", status=204)
        responses.get(
            API + "/users/alice/orgs/team/permissions",
            json={"is_owner": is_owner, "is_admin": True},
        )

    def mock_repo(self):
        responses.get(API + "/repos/team/repo", json=REPO)
        responses.get(API + "/repositories/41", json=REPO)

    def remote_issue(self, **changes):
        return {
            "id": 9000,
            "number": 7,
            "title": "Remote problem",
            "body": "Editable body",
            "html_url": BASE + "/team/repo/issues/7",
            "pull_request": None,
            # Issues the integration files are authored by the installing Gitea user.
            "user": {"id": USER["id"], "login": USER["login"]},
            **changes,
        }

    def issue_endpoint(self):
        return (
            f"/api/0/organizations/{self.organization.slug}/issues/{self.group.id}"
            f"/integrations/{self.integration.id}/"
        )


@no_silo_test
class TestIntegrationProvider(IntegrationFixture):
    def test_registration_serializer_and_features(self):
        provider = default_manager.get("gitea")
        assert isinstance(provider, GiteaIntegrationProvider)
        assert identity.exists("gitea")
        provider.setup()
        provider.setup()
        data = serialize(
            provider, serializer=IntegrationProviderSerializer(), organization=self.organization
        )
        assert data["canAdd"] is True
        assert provider.allow_multiple and provider.needs_default_identity
        assert provider.features == frozenset(
            {
                IntegrationFeatures.ISSUE_BASIC,
                IntegrationFeatures.ISSUE_SYNC,
                IntegrationFeatures.COMMITS,
                IntegrationFeatures.STACKTRACE_LINK,
                IntegrationFeatures.CODEOWNERS,
            }
        )
        from sentry.plugins.base import bindings

        from sentry_gitea.integration.repository import GiteaRepositoryProvider

        repository_providers = bindings.get("integration-repository.provider")
        assert repository_providers.get("integrations:gitea") is GiteaRepositoryProvider
        assert data["metadata"]["description"]

    @responses.activate
    def test_build_integration_and_reinstall_preserve_secret(self):
        self.mock_owners()
        state = {"identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)}, "owner": "org:8"}
        result = GiteaIntegrationProvider().build_integration(state)
        assert result["external_id"] == self.integration.external_id
        assert result["idp_external_id"] == result["external_id"]
        assert result["name"] == "team"
        assert result["metadata"] == METADATA
        assert result["user_identity"]["data"] == TOKENS
        assert result["user_identity"]["external_id"] == f"{BASE}|2"
        assert result["user_identity"]["scopes"] == list(OAUTH_SCOPES)
        assert TOKEN not in str(result["metadata"])

    @responses.activate
    def test_personal_install_and_new_secret(self):
        self.mock_owners()
        result = GiteaIntegrationProvider().build_integration(
            {"identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)}, "owner": "user:2"}
        )
        assert result["external_id"] == hashlib.sha256(f"v1|{BASE}|user|2".encode()).hexdigest()
        assert result["name"] == "alice"
        assert len(result["metadata"]["webhook_secret"]) == 64

    @responses.activate
    def test_owner_form_only_offers_self_and_owners_and_revalidates_post(self):
        self.mock_owners(is_owner=False)
        pipeline = FakePipeline({"identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)}})
        factory = RequestFactory()
        request = factory.get("/extensions/gitea/setup/")
        response = OwnerSelectionView().dispatch(request, pipeline)
        assert b"user:2" in response.content
        assert b"org:8" not in response.content
        assert b"csrfmiddlewaretoken" in response.content
        # The form is first rendered on ?code=...&state=...; browsers must not forward that
        # query to Sentry's CSRF failure logger through their Referer header.
        assert response["Referrer-Policy"] == "origin"
        request = factory.post("/extensions/gitea/setup/", {"owner": "org:8"})
        request._dont_enforce_csrf_checks = True
        with self.assertLogs("sentry_gitea.integration.provider", level="WARNING") as logs:
            response = OwnerSelectionView().dispatch(request, pipeline)
        assert not pipeline.advanced
        assert "owner" in response.content.decode().lower()
        assert TOKEN not in str(logs.output)

    @responses.activate
    def test_owner_form_post_requires_csrf(self):
        pipeline = FakePipeline({"identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)}})
        response = OwnerSelectionView().dispatch(
            RequestFactory().post("/extensions/gitea/setup/", {"owner": "user:2"}), pipeline
        )
        assert response.status_code == 403
        assert not responses.calls

    @responses.activate
    def test_builder_rejects_tampered_owner_without_echoing_input(self):
        self.mock_owners()
        with self.assertLogs("sentry_gitea.integration.provider", level="WARNING") as logs:
            with self.assertRaises(IntegrationError):
                GiteaIntegrationProvider().build_integration(
                    {
                        "identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)},
                        "owner": "user:999",
                    }
                )
        assert TOKEN not in str(logs.output)

    def test_defaults_merge_and_debug_metadata_excludes_secret(self):
        self.installation.update_organization_config(
            {"project_issue_defaults": {"1": {"repo": "x"}}}
        )
        self.installation.update_organization_config({"future_option": True})
        assert self.installation.get_config_data()["project_issue_defaults"]["1"]["repo"] == "x"
        assert "webhook_secret" not in self.installation.get_debug_metadata()

    def test_missing_default_identity_requires_reauthorization(self):
        self.org_integration.update(default_auth_id=None)
        with self.assertRaises(IntegrationError):
            self.integration.get_installation(self.organization.id).get_client()

    def test_missing_settings_is_setup_error_not_directory_error(self):
        with override_settings(GITEA_CLIENT_ID="", GITEA_CLIENT_SECRET="", GITEA_BASE_URL=""):
            provider = GiteaIntegrationProvider()
            assert provider.can_add
            pipeline = FakePipeline()
            with self.assertLogs("sentry_gitea.integration.identity", level="WARNING"):
                from sentry_gitea.integration.identity import OAuthAuthorizeView

                response = OAuthAuthorizeView().dispatch(RequestFactory().get("/"), pipeline)
            assert b"GITEA_BASE_URL" in response.content
            assert not pipeline.advanced


@no_silo_test
@override_settings(**SETTINGS)
class TestGiteaInstallPipeline(IntegrationTestCase):
    def setUp(self):
        super().setUp()
        feature = self.feature("organizations:integrations-issue-basic")
        feature.__enter__()
        self.addCleanup(feature.__exit__, None, None, None)
        self.client.handler.enforce_csrf_checks = True

    @property
    def provider(self):
        return default_manager.get("gitea")

    @responses.activate
    def test_generic_setup_callback_owner_submit_and_identity_persistence(self):
        responses.post(
            BASE + "/login/oauth/access_token",
            json={"access_token": TOKEN, "refresh_token": REFRESH, "expires_in": 3600},
        )
        responses.get(API + "/user", json=USER)
        responses.get(API + "/user/orgs", json=[])
        start = self.client.get(self.setup_path)
        assert start.status_code == 302
        query = parse_qs(urlsplit(start["Location"]).query)
        assert query["state"] and query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"][0].endswith(self.setup_path)
        form = self.client.get(
            self.setup_path, {"state": query["state"][0], "code": "private-code"}
        )
        assert form.status_code == 200
        assert form["Referrer-Policy"] == "origin"
        assert b"user:2" in form.content and b'name="csrfmiddlewaretoken"' in form.content
        csrf = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', form.content.decode())
        assert csrf is not None
        finish = self.client.post(
            self.setup_path, {"owner": "user:2", "csrfmiddlewaretoken": csrf[1]}
        )
        self.assertDialogSuccess(finish)
        installed = Integration.objects.get(provider="gitea", name="alice")
        association = OrganizationIntegration.objects.get(
            integration_id=installed.id, organization_id=self.organization.id
        )
        stored_identity = Identity.objects.get(id=association.default_auth_id)
        assert stored_identity.data["access_token"] == TOKEN
        assert stored_identity.data["refresh_token"] == REFRESH
        assert stored_identity.scopes == list(OAUTH_SCOPES)
        assert stored_identity.idp.external_id == installed.external_id
        assert TOKEN not in str(installed.metadata)
        assert "private-code" not in str(stored_identity.data)
        # The finished session cannot replay the code exchange.
        self.client.get(self.setup_path, {"state": query["state"][0], "code": "private-code"})
        assert len([call for call in responses.calls if call.request.method == "POST"]) == 1
