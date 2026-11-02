"""The per-installation repository selection: settings field, sync scope and deselect cleanup."""

from unittest.mock import patch

import responses
from django.test import override_settings
from sentry.constants import ObjectStatus
from sentry.deletions.tasks.scheduled import run_scheduled_deletions
from sentry.integrations.models.organization_integration import OrganizationIntegration
from sentry.models.repository import Repository
from sentry.testutils.silo import no_silo_test
from sync_fixtures import HOOKS, WEBHOOK_REPO, SyncFixture
from test_integration_provider import API

PREFIX = "https://hooks.example/sentry"
OTHER = {**WEBHOOK_REPO, "id": 43, "name": "other", "full_name": "team/other"}
READ_ONLY = {
    **WEBHOOK_REPO,
    "id": 42,
    "name": "read-only",
    "full_name": "team/read-only",
    "permissions": {"admin": False, "push": True, "pull": True},
}


def calls(method, url):
    return [c for c in responses.calls if c.request.method == method and c.request.url == url]


@no_silo_test
@override_settings(GITEA_WEBHOOK_URL_PREFIX=PREFIX)
class TestRepositorySelection(SyncFixture):
    def mock_owner_repositories(self, *rows):
        responses.get(
            API + "/repos/search",
            json={"data": list(rows)},
            headers={"X-Total-Count": str(len(rows))},
        )
        for row in rows:
            hooks = f"{API}/repos/{row['full_name']}/hooks"
            responses.get(f"{API}/repos/{row['full_name']}", json=row)
            responses.get(f"{API}/repositories/{row['id']}", json=row)
            responses.get(hooks, json=[], headers={"X-Total-Count": "0"})
            responses.post(hooks, json={"id": row["id"] + 100, "type": "gitea"}, status=201)

    def store(self, **config):
        self.org_integration.update(config={**self.org_integration.config, **config})
        self.installation = self.integration.get_installation(self.organization.id)

    def select(self, repository_ids):
        with self.tasks():
            self.installation.update_organization_config({"repository_ids": repository_ids})
        self.installation = self.integration.get_installation(self.organization.id)

    def sync(self):
        with self.tasks():
            response = self.client.post(
                f"/api/0/organizations/{self.organization.slug}/integrations/"
                f"{self.integration.id}/repo-sync/"
            )
        assert response.status_code == 202, response.data

    def active_external_ids(self):
        return sorted(
            Repository.objects.filter(
                organization_id=self.organization.id, status=ObjectStatus.ACTIVE
            ).values_list("external_id", flat=True)
        )

    @responses.activate
    def test_sync_imports_and_hooks_only_selected_repositories(self):
        self.mock_owner_repositories(WEBHOOK_REPO, OTHER)
        self.store(repository_ids=[41])
        assert [r["external_id"] for r in self.installation.get_repositories()] == ["41"]
        self.sync()
        assert self.active_external_ids() == ["41"]
        assert len(calls("POST", HOOKS)) == 1
        assert not calls("POST", API + "/repos/team/other/hooks")

    @responses.activate
    def test_installs_without_a_stored_selection_sync_every_repository(self):
        self.mock_owner_repositories(WEBHOOK_REPO, OTHER)
        assert "repository_ids" not in self.org_integration.config
        self.sync()
        assert self.active_external_ids() == ["41", "43"]

    @responses.activate
    def test_deselecting_removes_the_row_and_hook_and_selecting_again_restores_both(self):
        self.mock_owner_repositories(WEBHOOK_REPO, OTHER)
        responses.delete(HOOKS + "/4", status=204)
        kept = self.add_repo(hook_id=4)
        other = Repository.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            provider="integrations:gitea",
            name="team/other",
            external_id="43",
            config={"repo_id": 43, "path": "team/other", "hook_id": 143},
        )
        self.select([43])
        kept.refresh_from_db()
        assert kept.status == ObjectStatus.PENDING_DELETION
        assert self.active_external_ids() == ["43"]
        # Seer is not running in the test stack.
        with (
            self.tasks(),
            patch("sentry.deletions.defaults.repository.notify_seer_repository_deleted"),
        ):
            run_scheduled_deletions()
        assert not Repository.objects.filter(id=kept.id).exists()
        # Sentry's repository deletion runs on_delete_repository twice; Gitea answers 404 then.
        assert calls("DELETE", HOOKS + "/4")
        assert Repository.objects.get(id=other.id).status == ObjectStatus.ACTIVE

        self.select([41, 43])
        restored = Repository.objects.get(
            organization_id=self.organization.id, external_id="41", status=ObjectStatus.ACTIVE
        )
        assert restored.config["hook_id"] == 141
        assert len(calls("POST", HOOKS)) == 1

    @responses.activate
    @override_settings(GITEA_BLAME_ENABLED=True)
    def test_unselected_repositories_are_never_mirrored(self):
        self.mock_owner_repositories(WEBHOOK_REPO, OTHER)
        self.store(repository_ids=[41])
        with (
            patch("sentry_gitea.integration.tasks.mirror_repository.delay") as mirror,
            self.capture_on_commit_callbacks(execute=True),
        ):
            self.sync()
        [repository] = Repository.objects.filter(organization_id=self.organization.id)
        assert repository.external_id == "41"
        mirror.assert_called_once_with(repository_id=repository.id)

    @responses.activate
    def test_settings_offer_administered_repositories_and_save_the_selection(self):
        self.mock_owner_repositories(WEBHOOK_REPO, READ_ONLY, OTHER)
        endpoint = (
            f"/api/0/organizations/{self.organization.slug}/integrations/{self.integration.id}/"
        )
        response = self.client.get(endpoint)
        assert response.status_code == 200, response.data
        field = response.data["configOrganization"][0]
        assert (field["name"], field["type"], field["multiple"]) == (
            "repository_ids",
            "select",
            True,
        )
        assert field["choices"] == [[43, "team/other"], [41, "team/repo"]]
        assert field["default"] == [43, 41], "no stored selection is shown as every repository"

        with self.tasks():
            response = self.client.post(endpoint, {"repository_ids": ["41"]}, format="json")
        assert response.status_code == 200, response.data
        config = OrganizationIntegration.objects.get(id=self.org_integration.id).config
        assert config["repository_ids"] == [41]
        assert self.active_external_ids() == ["41"], "saving the selection starts a sync"
        response = self.client.get(endpoint)
        assert response.data["configData"]["repository_ids"] == [41]
        assert "default" not in response.data["configOrganization"][0]

        responses.replace(responses.GET, API + "/repos/search", status=500)
        field = self.client.get(endpoint).data["configOrganization"][0]
        assert field["disabled"] is True, "a list built without Gitea must not be saved"
        assert field["choices"] == [[41, "Repository 41"]]

        response = self.client.post(endpoint, {"repository_ids": "41"}, format="json")
        assert response.status_code == 400
        assert "Select repositories from the list." in response.data["detail"]
