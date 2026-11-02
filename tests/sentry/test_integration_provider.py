"""Original DB/Redis-only integration fixtures and installation tests."""

import hashlib
import re
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import responses
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from sentry import identity
from sentry.api.serializers import serialize
from sentry.constants import ObjectStatus
from sentry.integrations.api.serializers.models.integration import IntegrationProviderSerializer
from sentry.integrations.base import IntegrationFeatures
from sentry.integrations.manager import default_manager
from sentry.integrations.models.integration import Integration
from sentry.integrations.models.organization_integration import OrganizationIntegration
from sentry.models.repository import Repository
from sentry.shared_integrations.exceptions import IntegrationError
from sentry.testutils.cases import APITestCase, IntegrationTestCase
from sentry.testutils.silo import no_silo_test
from sentry.users.models.identity import Identity

from sentry_gitea.integration.provider import (
    GiteaIntegrationProvider,
    OwnerSelectionView,
    RepositorySelectionView,
)
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
INSTALL_PATH = "/extensions/gitea/install/"
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
        # Sentry 26.10's install modal cannot render out-of-tree providers.
        assert data["canAdd"] is False
        assert data["metadata"]["aspects"]["externalInstall"]["url"] == INSTALL_PATH
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
        state = {
            "identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)},
            "owner": "org:8",
            "repository_ids": {"offered": [41, 43], "ticked": [41], "reinstall": True},
        }
        result = GiteaIntegrationProvider().build_integration(state)
        assert result["post_install_data"] == {"repository_ids": state["repository_ids"]}
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
            {
                "identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)},
                "owner": "user:2",
                "repository_ids": {"offered": [], "ticked": [], "reinstall": False},
            }
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
    def test_owner_form_renders_in_sentry_layout_and_escapes_gitea_names(self):
        self.mock_owners()
        pipeline = FakePipeline({"identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)}})
        hostile = [{"kind": "org", "id": 8, "login": '<img src=x onerror="alert(1)">'}]
        request = RequestFactory().get("/extensions/gitea/setup/")
        with (
            patch(
                "sentry_gitea.integration.provider.available_owners",
                return_value=(USER, hostile),
            ),
            self.assertTemplateUsed("sentry/bases/modal.html"),
        ):
            response = OwnerSelectionView().dispatch(request, pipeline)
        page = response.content.decode()
        assert response.status_code == 200
        assert '<option value="org:8">&lt;img src=x onerror=&quot;alert(1)&quot;&gt; (org)' in page
        assert "<img src=x" not in page
        assert 'class="btn btn-primary"' in page
        assert response["Cache-Control"] == "no-store"
        assert response["Referrer-Policy"] == "origin"

    @responses.activate
    def test_owner_form_answers_other_methods_with_405(self):
        self.mock_owners()
        pipeline = FakePipeline({"identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)}})
        request = RequestFactory().put("/extensions/gitea/setup/")
        request._dont_enforce_csrf_checks = True
        assert OwnerSelectionView().dispatch(request, pipeline).status_code == 405
        assert not pipeline.advanced

    @responses.activate
    def test_owner_form_post_requires_csrf(self):
        pipeline = FakePipeline({"identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)}})
        response = OwnerSelectionView().dispatch(
            RequestFactory().post("/extensions/gitea/setup/", {"owner": "user:2"}), pipeline
        )
        assert response.status_code == 403
        assert not responses.calls

    def picker(self, organization=None):
        pipeline = FakePipeline(
            {"identity": {"tokens": TOKENS, "scopes": list(OAUTH_SCOPES)}, "owner": "org:8"}
        )
        pipeline.organization = organization or self.create_organization(owner=self.user)
        return pipeline

    def mock_owner_repositories(self):
        administered = {**REPO, "permissions": {"admin": True}}
        hostile = {**administered, "description": '<img src=x onerror="alert(1)">'}
        read_only = {**REPO, "id": 42, "full_name": "team/read-only", "permissions": {}}
        responses.get(
            API + "/repos/search",
            json={"data": [hostile, read_only]},
            headers={"X-Total-Count": "2"},
        )

    @responses.activate
    def test_repository_form_lists_administered_repositories_escaped_in_sentry_layout(self):
        self.mock_owner_repositories()
        request = RequestFactory().get("/extensions/gitea/setup/")
        with self.assertTemplateUsed("sentry/bases/modal.html"):
            response = RepositorySelectionView().dispatch(request, self.picker())
        page = response.content.decode()
        assert response.status_code == 200
        assert re.findall(r'name="repository" value="([^"]+)"', page) == ["41"]
        assert "team/repo" in page and "team/read-only" not in page
        assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page
        assert "<img src=x" not in page
        assert "csrfmiddlewaretoken" in page
        assert response["Cache-Control"] == "no-store"
        assert response["Referrer-Policy"] == "origin"
        assert parse_qs(urlsplit(responses.calls[0].request.url).query)["uid"] == ["8"]

    @responses.activate
    def test_repository_form_binds_the_selection_and_rejects_other_repositories(self):
        self.mock_owner_repositories()

        def submit(pipeline, data):
            request = RequestFactory().post("/extensions/gitea/setup/", data)
            request._dont_enforce_csrf_checks = True
            return RepositorySelectionView().dispatch(request, pipeline)

        for chosen in (["42"], ["41", "999"], ["x"]):
            pipeline = self.picker()
            with self.assertLogs("sentry_gitea.integration.provider", level="WARNING"):
                response = submit(pipeline, {"step": "repositories", "repository": chosen})
            assert response.status_code == 400 and not pipeline.advanced
        pipeline = self.picker()
        response = submit(pipeline, {"owner": "org:8"})
        assert b'name="repository" value="41"' in response.content, "the owner POST renders it"
        assert not pipeline.advanced
        submit(pipeline, {"step": "repositories", "repository": ["41"]})
        assert pipeline.advanced
        assert pipeline.data["repository_ids"] == {
            "offered": [41],
            "ticked": [41],
            "reinstall": False,
        }

    @responses.activate
    def test_repository_form_ticks_the_stored_selection_of_a_reinstall(self):
        administered = {**REPO, "permissions": {"admin": True}}
        other = {**administered, "id": 43, "full_name": "team/other"}
        responses.get(
            API + "/repos/search",
            json={"data": [administered, other]},
            headers={"X-Total-Count": "2"},
        )

        def ticked(organization):
            response = RepositorySelectionView().dispatch(
                RequestFactory().get("/extensions/gitea/setup/"), self.picker(organization)
            )
            page = response.content.decode()
            assert ("already installed" in page) == (organization == self.organization)
            return re.findall(r'name="repository" value="(\d+)" checked', page)

        assert ticked(self.create_organization(owner=self.user)) == [], "a new install"
        assert ticked(self.organization) == ["43", "41"], "no stored selection means all"
        self.org_integration.update(config={"repository_ids": [41, 99]})
        assert ticked(self.organization) == ["41"]
        self.org_integration.update(config={"repository_ids": []})
        assert ticked(self.organization) == []

    @responses.activate
    def test_repository_form_answers_other_methods_with_405_and_requires_csrf(self):
        self.mock_owner_repositories()
        request = RequestFactory().put("/extensions/gitea/setup/")
        request._dont_enforce_csrf_checks = True
        pipeline = self.picker()
        assert RepositorySelectionView().dispatch(request, pipeline).status_code == 405
        responses.calls.reset()
        request = RequestFactory().post(
            "/extensions/gitea/setup/", {"step": "repositories", "repository": ["41"]}
        )
        assert RepositorySelectionView().dispatch(request, pipeline).status_code == 403
        assert not pipeline.advanced and not responses.calls

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
            assert provider.metadata.aspects["externalInstall"]
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
        start = self.client.get(INSTALL_PATH, {"organization": self.organization.slug})
        assert start.status_code == 302
        query = parse_qs(urlsplit(start["Location"]).query)
        assert query["state"] and query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"][0].endswith(self.setup_path)
        form = self.client.get(
            self.setup_path, {"state": query["state"][0], "code": "private-code"}
        )
        assert form.status_code == 200
        assert form["Referrer-Policy"] == "origin"
        assert "sentry/bases/modal.html" in [t.name for t in form.templates]
        assert b"user:2" in form.content and b'name="csrfmiddlewaretoken"' in form.content
        csrf = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', form.content.decode())
        assert csrf is not None
        personal = {
            **REPO,
            "full_name": "alice/repo",
            "owner": {"id": 2, "login": "alice"},
            "permissions": {"admin": True},
        }
        responses.get(
            API + "/repos/search",
            json={"data": [personal, {**personal, "id": 43, "full_name": "alice/other"}]},
            headers={"X-Total-Count": "2"},
        )
        picker = self.client.post(
            self.setup_path, {"owner": "user:2", "csrfmiddlewaretoken": csrf[1]}
        )
        assert picker.status_code == 200
        assert picker["Referrer-Policy"] == "origin" and picker["Cache-Control"] == "no-store"
        assert re.findall(r'name="repository" value="(\d+)"', picker.content.decode()) == [
            "43",
            "41",
        ]
        rejected = self.client.post(self.setup_path, {"step": "repositories", "repository": ["43"]})
        assert rejected.status_code == 403, "the repository form is CSRF-protected"
        with patch(
            "sentry.integrations.source_code_management.sync_repos.sync_repos_for_org.apply_async"
        ) as first_sync:
            finish = self.client.post(
                self.setup_path,
                {"step": "repositories", "repository": ["43"], "csrfmiddlewaretoken": csrf[1]},
            )
        self.assertDialogSuccess(finish)
        installed = Integration.objects.get(provider="gitea", name="alice")
        association = OrganizationIntegration.objects.get(
            integration_id=installed.id, organization_id=self.organization.id
        )
        assert association.config["repository_ids"] == [43]
        first_sync.assert_called_once_with(kwargs={"organization_integration_id": association.id})
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

    def install(self, choose):
        start = self.client.get(INSTALL_PATH, {"organization": self.organization.slug})
        state = parse_qs(urlsplit(start["Location"]).query)["state"][0]
        form = self.client.get(self.setup_path, {"state": state, "code": "private-code"})
        csrf = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', form.content.decode())[1]
        picker = self.client.post(self.setup_path, {"owner": "user:2", "csrfmiddlewaretoken": csrf})
        page = picker.content.decode()
        with patch(
            "sentry.integrations.source_code_management.sync_repos.sync_repos_for_org.apply_async"
        ):
            finish = self.client.post(
                self.setup_path,
                {"step": "repositories", "repository": choose(page), "csrfmiddlewaretoken": csrf},
            )
        self.assertDialogSuccess(finish)
        return page

    @responses.activate
    def test_a_reinstall_keeps_the_selection_unless_the_user_unticks_a_repository(self):
        responses.post(
            BASE + "/login/oauth/access_token",
            json={"access_token": TOKEN, "refresh_token": REFRESH, "expires_in": 3600},
        )
        responses.get(API + "/user", json=USER)
        responses.get(API + "/user/orgs", json=[])
        personal = {**REPO, "owner": {"id": 2, "login": "alice"}, "permissions": {"admin": True}}
        responses.get(
            API + "/repos/search",
            json={
                "data": [
                    {**personal, "full_name": "alice/repo"},
                    {**personal, "id": 43, "full_name": "alice/other"},
                ]
            },
            headers={"X-Total-Count": "2"},
        )
        self.install(lambda page: ["41"])
        installed = Integration.objects.get(provider="gitea", name="alice")
        association = OrganizationIntegration.objects.get(integration_id=installed.id)
        # 50 was selected by another admin; this user's picker does not list it.
        association.update(config={**association.config, "repository_ids": [41, 50]})
        rows = {
            repo_id: Repository.objects.create(
                organization_id=self.organization.id,
                integration_id=installed.id,
                provider="integrations:gitea",
                name=f"alice/{repo_id}",
                external_id=str(repo_id),
                config={"repo_id": repo_id, "hook_id": repo_id + 100},
            )
            for repo_id in (41, 50)
        }

        def statuses():
            return {k: Repository.objects.get(id=r.id).status for k, r in rows.items()}

        def selection():
            return OrganizationIntegration.objects.get(id=association.id).config.get(
                "repository_ids", "every repository"
            )

        def as_rendered(page):
            return re.findall(r'name="repository" value="(\d+)" checked', page)

        page = self.install(as_rendered)
        assert as_rendered(page) == ["41"] and "already installed" in page
        assert selection() == [41, 50]
        assert statuses() == {41: ObjectStatus.ACTIVE, 50: ObjectStatus.ACTIVE}

        association.update(config={})
        assert as_rendered(self.install(as_rendered)) == ["43", "41"]
        assert selection() == "every repository", "a key-less install stays key-less"
        self.install(lambda page: ["41"])
        assert selection() == [41, 50], "unticking 43 keeps every imported repository"

        self.install(lambda page: [])
        assert selection() == [50], "only an untick deselects"
        assert statuses() == {41: ObjectStatus.PENDING_DELETION, 50: ObjectStatus.ACTIVE}

    def test_install_page_lets_the_user_choose_among_permitted_organizations(self):
        other = self.create_organization(name="<b>bar</b>", owner=self.user)
        member_only = self.create_organization(name="baz")
        self.create_member(organization=member_only, user=self.user, role="member")
        with self.assertTemplateUsed("sentry/bases/modal.html"):
            choice = self.client.get(INSTALL_PATH)
        assert choice.status_code == 200
        assert choice["Cache-Control"] == "no-store"
        page = choice.content.decode()
        assert {self.organization.slug, other.slug} == set(
            re.findall(r'<option value="([^"]+)"', page)
        )
        assert "&lt;b&gt;bar&lt;/b&gt;" in page and "<b>bar</b>" not in page
        self.client.handler.enforce_csrf_checks = False
        assert self.client.post(INSTALL_PATH).status_code == 405
        assert self.client.get(INSTALL_PATH, {"organization": member_only.slug}).status_code == 200
        assert not responses.calls

    def test_install_requires_an_organization_the_user_may_install_into(self):
        self.login_as(self.create_user())
        assert self.client.get(INSTALL_PATH).status_code == 403

    def test_setup_without_a_started_install_is_rejected(self):
        response = self.client.get(self.setup_path, {"state": "x", "code": "y"})
        assert response.status_code == 400
        assert not responses.calls
