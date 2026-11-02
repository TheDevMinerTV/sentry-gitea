"""Add Repository (real endpoint), the signed hook lifecycle and release commit ranges."""

import json
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import responses
from django.db import IntegrityError
from django.test import override_settings
from sentry.constants import ObjectStatus
from sentry.models.commitfilechange import CommitFileChange
from sentry.models.releasecommit import ReleaseCommit
from sentry.models.repository import Repository
from sentry.shared_integrations.exceptions import IntegrationError
from sentry.tasks.commits import fetch_commits
from sentry.testutils.silo import no_silo_test
from sync_fixtures import (
    COMMITS,
    HOOKS,
    REPO_SECRET,
    SHA_A,
    SHA_B,
    WEBHOOK_REPO,
    SyncFixture,
    api_commit,
    gitea_hook,
    sha,
)
from test_integration_provider import API, METADATA, PUBLIC

from sentry_gitea.integration.repository import GiteaRepositoryProvider

PREFIX = "https://hooks.example/sentry"
EVENTS = ["push", "issues_only", "issue_assign", "pull_request"]


def requests(method):
    return [call for call in responses.calls if call.request.method == method]


@no_silo_test
@override_settings(GITEA_WEBHOOK_URL_PREFIX=PREFIX)
class TestAddRepository(SyncFixture):
    def gitea_hook_url(self, repo_id=41):
        return f"{PREFIX}/extensions/gitea/webhook/{self.integration.id}/{repo_id}/"

    def add(self, identifier="team/repo", **data):
        return self.client.post(
            f"/api/0/organizations/{self.organization.slug}/repos/",
            {
                "provider": "integrations:gitea",
                "installation": self.integration.id,
                "identifier": identifier,
                **data,
            },
            format="json",
        )

    @responses.activate
    def test_add_repository_creates_one_signed_hook(self):
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        responses.get(HOOKS, json=[], headers={"X-Total-Count": "0"})
        responses.post(HOOKS, json={"id": 4, "type": "gitea"}, status=201)
        result = self.add()
        assert result.status_code == 201, result.data
        assert result.data["provider"]["id"] == "integrations:gitea"
        assert result.data["externalSlug"] == "team/repo"
        repo = Repository.objects.get(organization_id=self.organization.id)
        assert (repo.name, repo.external_id, repo.url) == ("team/repo", "41", PUBLIC + "/team/repo")
        assert (repo.provider, repo.integration_id) == ("integrations:gitea", self.integration.id)
        assert repo.config == {
            "repo_id": 41,
            "path": "team/repo",
            "hook_id": 4,
            "default_branch": "main",
        }
        [post] = requests("POST")
        assert json.loads(post.request.body) == {
            "type": "gitea",
            "active": True,
            "events": EVENTS,
            "branch_filter": "",
            "config": {
                "url": self.gitea_hook_url(),
                "content_type": "json",
                "secret": REPO_SECRET,
            },
        }
        assert REPO_SECRET != METADATA["webhook_secret"]

    @responses.activate
    def test_repository_sync_adds_owner_repositories_with_one_signed_hook(self):
        responses.get(
            API + "/repos/search", json={"data": [WEBHOOK_REPO]}, headers={"X-Total-Count": "1"}
        )
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        responses.get(HOOKS, json=[], headers={"X-Total-Count": "0"})
        responses.post(HOOKS, json={"id": 4, "type": "gitea"}, status=201)
        with self.tasks():
            result = self.client.post(
                f"/api/0/organizations/{self.organization.slug}/integrations/"
                f"{self.integration.id}/repo-sync/"
            )
        assert result.status_code == 202, result.data
        repo = Repository.objects.get(organization_id=self.organization.id)
        assert (repo.name, repo.external_id, repo.status) == (
            "team/repo",
            "41",
            ObjectStatus.ACTIVE,
        )
        assert repo.config["hook_id"] == 4
        [post] = requests("POST")
        assert json.loads(post.request.body)["config"]["url"] == self.gitea_hook_url()

    @responses.activate
    @override_settings(GITEA_WEBHOOK_URL_PREFIX=None)
    def test_webhook_prefix_defaults_to_system_url_prefix(self):
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        responses.get(HOOKS, json=[], headers={"X-Total-Count": "0"})
        responses.post(HOOKS, json={"id": 4}, status=201)
        with self.options({"system.url-prefix": "https://sentry.example/"}):
            assert self.add().status_code == 201
        [post] = requests("POST")
        url = json.loads(post.request.body)["config"]["url"]
        assert url == f"https://sentry.example/extensions/gitea/webhook/{self.integration.id}/41/"

    @responses.activate
    def test_adding_again_reuses_recorded_hook_and_replaces_unknown_duplicates(self):
        repo = self.add_repo()
        legacy = f"{PREFIX}/extensions/gitea/webhook/{self.integration.id}/"
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        responses.get(
            HOOKS,
            json=[
                gitea_hook(4, self.gitea_hook_url()),
                gitea_hook(5, self.gitea_hook_url()),  # unknown secret
                gitea_hook(6, legacy),  # the integration-wide secret of older versions
                gitea_hook(7, "https://chat.example", "slack"),
                gitea_hook(8, "https://ci.example/hook"),  # someone else's
            ],
            headers={"X-Total-Count": "5"},
        )
        responses.patch(HOOKS + "/4", json=gitea_hook(4, self.gitea_hook_url()))
        responses.delete(HOOKS + "/5", status=204)
        responses.delete(HOOKS + "/6", status=204)
        assert self.add().status_code == 201
        assert not requests("POST")
        assert sorted(c.request.url for c in requests("DELETE")) == [HOOKS + "/5", HOOKS + "/6"]
        patched = json.loads(requests("PATCH")[0].request.body)
        # PATCH cannot change a secret; the reused hook keeps the one it was created with.
        assert patched["config"] == {"url": self.gitea_hook_url(), "content_type": "json"}
        assert patched["active"] is True and patched["events"] == EVENTS
        assert Repository.objects.get().id == repo.id
        repo.refresh_from_db()
        assert repo.config["hook_id"] == 4 and repo.status == ObjectStatus.ACTIVE

    @responses.activate
    def test_hooks_with_another_url_are_recreated_for_every_sharing_organization(self):
        other = self.create_organization(owner=self.user)
        self.integration.add_organization(other, self.user, default_auth_id=self.identity.id)
        mine, theirs = self.add_repo(), self.add_repo(organization=other)
        legacy = f"{PREFIX}/extensions/gitea/webhook/{self.integration.id}/"
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        responses.get(HOOKS, json=[gitea_hook(4, legacy)], headers={"X-Total-Count": "1"})
        responses.delete(HOOKS + "/4", status=204)
        responses.post(HOOKS, json=gitea_hook(9, self.gitea_hook_url()), status=201)
        assert self.add().status_code == 201
        assert not requests("PATCH")  # a secret cannot be changed in place
        [post] = requests("POST")
        assert json.loads(post.request.body)["config"] == {
            "url": self.gitea_hook_url(),
            "content_type": "json",
            "secret": REPO_SECRET,
        }
        for row in (mine, theirs):
            row.refresh_from_db()
            assert row.config["hook_id"] == 9
        # Deleting one organization's row now keeps the hook the other one still uses.
        GiteaRepositoryProvider("integrations:gitea").on_delete_repository(mine)
        assert len(requests("DELETE")) == 1

    @responses.activate
    def test_bulk_mapping_and_hidden_rows_are_repaired_in_place(self):
        repo = self.add_repo(hook_id=None)
        repo.update(external_id="team/repo", config={}, url=None, status=ObjectStatus.HIDDEN)
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        responses.get(HOOKS, json=[])
        responses.post(HOOKS, json={"id": 7}, status=201)
        assert self.add().status_code == 201
        repo.refresh_from_db()
        assert Repository.objects.count() == 1
        assert (repo.external_id, repo.url, repo.status) == ("41", PUBLIC + "/team/repo", 0)
        assert repo.config["hook_id"] == 7 and repo.config["repo_id"] == 41

    @responses.activate
    def test_admin_permission_is_required(self):
        responses.get(
            API + "/repos/team/repo",
            json={**WEBHOOK_REPO, "permissions": {"admin": False, "push": True}},
        )
        result = self.add()
        assert result.status_code == 400
        assert "admin permission" in result.data["errors"]["__all__"]
        assert [c.request.method for c in responses.calls] == ["GET"]
        assert not Repository.objects.exists()

    @responses.activate
    def test_wrong_owner_invalid_identifier_and_foreign_installation(self):
        responses.get(API + "/repos/other/repo", json={**WEBHOOK_REPO, "owner": {"id": 99}})
        assert self.add("other/repo").status_code == 400
        assert self.add("../repo").status_code == 400
        other = self.create_organization(owner=self.user)
        result = self.client.post(
            f"/api/0/organizations/{other.slug}/repos/",
            {
                "provider": "integrations:gitea",
                "installation": self.integration.id,
                "identifier": "team/repo",
            },
            format="json",
        )
        assert result.status_code == 404
        assert not Repository.objects.exists()
        assert not requests("POST")

    @responses.activate
    def test_hook_failure_creates_no_repository(self):
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        responses.get(HOOKS, json=[])
        responses.post(HOOKS, status=403, json={"message": "forbidden"})
        with self.assertLogs("sentry_gitea.integration.repository", level="WARNING") as logs:
            result = self.add()
        assert result.status_code == 400
        assert "denied access" in result.data["errors"]["__all__"]
        assert logs.records[0].getMessage() == "sentry-gitea: sync failed"
        assert METADATA["webhook_secret"] not in "\n".join(logs.output)
        assert not Repository.objects.exists()

    @responses.activate
    def test_database_failure_removes_the_new_hook(self):
        responses.get(API + "/repos/team/repo", json=WEBHOOK_REPO)
        responses.get(HOOKS, json=[])
        responses.post(HOOKS, json={"id": 4}, status=201)
        responses.delete(HOOKS + "/4", status=204)
        with patch.object(Repository.objects, "create", side_effect=IntegrityError("race")):
            result = self.add()
        assert result.status_code == 500
        assert [c.request.method for c in responses.calls][-1] == "DELETE"


@no_silo_test
class TestRepositoryDeletion(SyncFixture):
    @responses.activate
    def test_deleting_the_repository_removes_its_hook(self):
        from sentry.deletions.tasks.scheduled import run_scheduled_deletions

        repo = self.add_repo()
        result = self.client.delete(
            f"/api/0/organizations/{self.organization.slug}/repos/{repo.id}/"
        )
        assert result.status_code == 202
        repo.refresh_from_db()
        assert repo.external_id != "41"  # renamed while pending deletion
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        responses.delete(HOOKS + "/4", status=204)
        # Sentry 26.10 also tells Seer about the deletion; there is no Seer here.
        with (
            self.tasks(),
            patch("sentry.deletions.defaults.repository.notify_seer_repository_deleted"),
        ):
            run_scheduled_deletions()
        assert not Repository.objects.filter(id=repo.id).exists()
        # Sentry signals pending_delete on both deletion passes; removal is idempotent.
        deletes = {c.request.url for c in requests("DELETE")}
        assert deletes == {HOOKS + "/4"}

    @responses.activate
    def test_shared_hook_is_kept_until_the_last_repository_and_missing_is_success(self):
        provider = GiteaRepositoryProvider("integrations:gitea")
        other = self.create_organization(owner=self.user)
        self.integration.add_organization(other, self.user, default_auth_id=self.identity.id)
        first, second = self.add_repo(), self.add_repo(organization=other)
        provider.on_delete_repository(first)
        assert not responses.calls
        first.update(status=ObjectStatus.PENDING_DELETION)
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        responses.delete(HOOKS + "/4", status=404)  # already removed in Gitea
        provider.on_delete_repository(second)
        assert responses.calls[-1].request.method == "DELETE"
        responses.replace(responses.GET, API + "/repositories/41", status=404)
        provider.on_delete_repository(second)  # repository gone: nothing to delete
        assert len(requests("DELETE")) == 1

    @responses.activate
    def test_hook_removal_failure_is_reported(self):
        repo = self.add_repo()
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        responses.delete(HOOKS + "/4", status=500)
        with self.assertRaises(IntegrationError):
            GiteaRepositoryProvider("integrations:gitea").on_delete_repository(repo)

    def test_rows_without_hooks_need_no_http(self):
        repo = self.add_repo(hook_id=None)
        GiteaRepositoryProvider("integrations:gitea").on_delete_repository(repo)


def range_callback(rows, total):
    def callback(request):
        params = parse_qs(urlsplit(request.url).query)
        page, limit = int(params["page"][0]), int(params["limit"][0])
        body = rows[(page - 1) * limit : page * limit]
        return 200, {"X-Total-Count": str(total)}, json.dumps(body)

    return callback


@no_silo_test
class TestReleaseCommits(SyncFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.add_repo()
        self.provider = GiteaRepositoryProvider("integrations:gitea")

    @responses.activate
    def test_full_range_with_file_patch_sets(self):
        rows = [
            api_commit(sha(3), [{"filename": "a.py", "status": "added"}]),
            api_commit(sha(2), [{"filename": "b.py", "status": "removed"}]),
            api_commit(sha(1), [{"filename": "c.py", "status": "modified"}]),
        ]
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        responses.add_callback(responses.GET, COMMITS, callback=range_callback(rows, 3))
        commits = self.provider.compare_commits(self.repo, SHA_A, SHA_B)
        assert [c["id"] for c in commits] == [sha(3), sha(2), sha(1)]
        assert commits[0]["repository"] == "team/repo"
        assert commits[0]["author_email"] == "author@example.com"
        assert [c["patch_set"] for c in commits] == [
            [{"path": "a.py", "type": "A"}],
            [{"path": "b.py", "type": "D"}],
            [{"path": "c.py", "type": "M"}],
        ]
        params = parse_qs(urlsplit(responses.calls[-1].request.url).query)
        assert params["sha"] == [SHA_B] and params["not"] == [SHA_A]

    @responses.activate
    def test_ranges_beyond_five_thousand_fail_visibly(self):
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        rows = [api_commit(sha(i)) for i in range(1, 51)]
        responses.add_callback(responses.GET, COMMITS, callback=range_callback(rows, 5001))
        with self.assertRaisesMessage(IntegrationError, "more than 5,000 commits"):
            self.provider.compare_commits(self.repo, SHA_A, SHA_B)
        assert len([c for c in responses.calls if "/commits" in c.request.url]) == 1

    @responses.activate
    def test_missing_start_imports_the_last_twenty_commits(self):
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        rows = [api_commit(sha(i)) for i in range(1, 101)]
        responses.add_callback(responses.GET, COMMITS, callback=range_callback(rows, 100))
        commits = self.provider.compare_commits(self.repo, None, SHA_B)
        assert len(commits) == 20 and commits[0]["id"] == sha(1)
        assert "not" not in parse_qs(urlsplit(responses.calls[-1].request.url).query)

    @responses.activate
    def test_release_fetch_commits_task_records_the_range(self):
        rows = [api_commit(sha(i), [{"filename": f"f{i}.py", "status": "added"}]) for i in (2, 1)]
        responses.get(API + "/repositories/41", json=WEBHOOK_REPO)
        responses.add_callback(responses.GET, COMMITS, callback=range_callback(rows, 2))
        release = self.create_release(project=self.project, version="1.0.0")
        refs = [{"repository": "team/repo", "commit": SHA_B, "previousCommit": SHA_A}]
        with self.tasks():
            fetch_commits(release.id, self.user.id, refs)
        recorded = ReleaseCommit.objects.filter(release=release).order_by("order")
        assert [rc.commit.key for rc in recorded] == [sha(2), sha(1)]
        assert CommitFileChange.objects.filter(filename="f1.py", type="A").exists()

    def test_pull_request_url_and_external_slug(self):
        pull = type("PR", (), {"key": "12"})()
        assert self.provider.pull_request_url(self.repo, pull) == PUBLIC + "/team/repo/pulls/12"
        assert self.provider.repository_external_slug(self.repo) == "team/repo"
        with self.assertRaises(IntegrationError):
            self.provider.compare_commits(self.repo, "bad ref", SHA_B)
