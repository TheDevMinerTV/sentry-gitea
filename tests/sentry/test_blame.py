"""Blame-based suspect commits from local mirrors, against a real repository served over HTTP."""

import functools
import os
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import responses
from django.test import override_settings
from django.utils import timezone
from git_fixtures import ALICE, BOB, GIT_AVAILABLE, serve, source_repo
from sentry.deletions.tasks.scheduled import run_scheduled_deletions
from sentry.integrations.source_code_management.commit_context import (
    CommitContextIntegration,
    SourceLineInfo,
)
from sentry.integrations.utils.commit_context import find_commit_context_for_event_all_frames
from sentry.models.commit import Commit
from sentry.models.groupowner import GroupOwner, GroupOwnerType
from sentry.tasks import post_process
from sentry.tasks.commit_context import process_commit_context
from sentry.testutils.silo import no_silo_test
from sync_fixtures import WEBHOOK_REPO, SyncFixture

from sentry_gitea.integration.provider import GiteaIntegrationProvider
from sentry_gitea.integration.repository import GiteaRepositoryProvider
from sentry_gitea.integration.tasks import mirror_repository
from sentry_gitea.mirror import FETCHED_STAMP, mirror_path

TOKEN = "synthetic-integration-access"


def blame_settings(test):
    # Settings overridden in setUp did not reach the test methods of these Sentry test cases,
    # and the server URL is only known per test.
    @functools.wraps(test)
    def wrapper(self, *args, **kwargs):
        with override_settings(
            GITEA_BASE_URL=self.server.url,
            GITEA_BLAME_ENABLED=True,
            GITEA_BLAME_MIRROR_DIR=str(self.mirrors),
        ):
            return test(self, *args, **kwargs)

    return wrapper


@no_silo_test
@pytest.mark.skipif(not GIT_AVAILABLE, reason="git is not installed in the test image")
class TestBlame(SyncFixture):
    def setUp(self):
        super().setUp()
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        root = Path(workdir.name)
        now = timezone.now()
        self.source = source_repo(root / "src", now - timedelta(days=3), now - timedelta(days=2))
        self.server = self.enterContext(serve(root / "src" / "served"))
        self.mirrors = root / "mirrors"
        self.integration.update(metadata={**self.integration.metadata, "base_url": self.server.url})
        self.installation = self.integration.get_installation(self.organization.id)
        self.repo = self.add_repo()
        self.code_mapping = self.create_code_mapping(
            project=self.project,
            repo=self.repo,
            organization_integration=self.org_integration,
            default_branch="main",
        )
        self.enterContext(
            patch.object(
                post_process,
                "COMMIT_CONTEXT_INTEGRATION_PROVIDERS",
                list(post_process.COMMIT_CONTEXT_INTEGRATION_PROVIDERS),
            )
        )

    def mock_lookup(self):
        responses.get(self.server.url + "/api/v1/repositories/41", json=WEBHOOK_REPO)

    def line(self, lineno, path="app.py", ref="main"):
        return SourceLineInfo(
            lineno=lineno, path=path, ref=ref, repo=self.repo, code_mapping=self.code_mapping
        )

    @responses.activate
    @blame_settings
    def test_blame_for_files_reads_a_real_repository_through_the_mirror(self):
        self.mock_lookup()
        assert isinstance(self.installation, CommitContextIntegration)
        files = [
            self.line(2),
            self.line(1),
            self.line(99),
            self.line(1, path="missing.py"),
            self.line(1, ref="no-such-branch"),
        ]
        with self.tasks():
            blamed = self.installation.get_blame_for_files(files, extra={})
        assert [(b.lineno, b.path, b.ref, b.commit.commitId) for b in blamed] == [
            (2, "app.py", "main", self.source.commits["Fix b"]),
            (1, "app.py", "main", self.source.commits["Add app"]),
        ]
        fix = blamed[0].commit
        assert (fix.commitAuthorName, fix.commitAuthorEmail) == BOB
        assert fix.commitMessage == "Fix b"
        assert fix.committedDate.tzinfo is not None
        assert (blamed[1].commit.commitAuthorEmail, blamed[0].code_mapping) == (
            ALICE[1],
            self.code_mapping,
        )
        assert set(self.server.authorizations) == {f"Bearer {TOKEN}"}
        stored = [p for p in self.mirrors.rglob("*") if p.is_file()]
        assert stored and not [p for p in stored if TOKEN.encode() in p.read_bytes()]

    @responses.activate
    @blame_settings
    def test_a_known_commit_is_blamed_without_contacting_gitea(self):
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        # Only the known-commit shortcut, not the fetch throttle, may keep this offline.
        stamp = mirror_path(self.mirrors, self.integration.id, 41) / FETCHED_STAMP
        os.utime(stamp, (0, 0))
        requests = len(self.server.authorizations), len(responses.calls)
        commit = self.source.commits["Add app"]
        blamed = self.installation.get_blame_for_files([self.line(2, ref=commit)], extra={})
        assert [b.commit.commitId for b in blamed] == [commit]
        assert (len(self.server.authorizations), len(responses.calls)) == requests

    @responses.activate
    @blame_settings
    def test_a_push_makes_the_next_branch_blame_see_the_pushed_commit(self):
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        pushed = self.source.commit(
            "Rework b", {"app.py": "a\nbb\nc\n"}, author=ALICE, when=timezone.now()
        )
        assert self.signed("push", self.push_payload()).status_code == 204
        blamed = self.installation.get_blame_for_files([self.line(2)], extra={})
        assert [b.commit.commitId for b in blamed] == [pushed]

    @blame_settings
    def test_setup_registers_gitea_for_commit_context_once_and_only_when_enabled(self):
        providers = post_process.COMMIT_CONTEXT_INTEGRATION_PROVIDERS
        with override_settings(GITEA_BLAME_ENABLED=False):
            GiteaIntegrationProvider().setup()
        assert "gitea" not in providers
        GiteaIntegrationProvider().setup()
        GiteaIntegrationProvider().setup()
        assert providers.count("gitea") == 1
        assert post_process._project_has_usable_code_mapping(self.project)

    @responses.activate
    @blame_settings
    def test_commit_context_task_creates_the_suspect_commit(self):
        self.mock_lookup()
        GiteaIntegrationProvider().setup()
        assert post_process._project_has_usable_code_mapping(self.project)
        group = self.create_group(project=self.project, first_seen=timezone.now())
        frames = [{"filename": "app.py", "abs_path": "app.py", "lineno": 2, "in_app": True}]
        with self.tasks():
            process_commit_context(
                event_id="a" * 32,
                event_platform="python",
                event_frames=frames,
                group_id=group.id,
                project_id=self.project.id,
            )
        owner = GroupOwner.objects.get(group=group, type=GroupOwnerType.SUSPECT_COMMIT.value)
        commit = Commit.objects.get(id=owner.context["commitId"])
        assert commit.key == self.source.commits["Fix b"]
        assert commit.repository_id == self.repo.id
        assert commit.message == "Fix b"
        assert commit.author.email == BOB[1]

    @responses.activate
    @blame_settings
    def test_deleting_the_last_repository_removes_its_mirror(self):
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        path = mirror_path(self.mirrors, self.integration.id, 41)
        assert path.is_dir()
        provider = GiteaRepositoryProvider("integrations:gitea")
        other = self.create_organization(owner=self.user)
        self.integration.add_organization(other, self.user, default_auth_id=self.identity.id)
        shared = self.add_repo(organization=other, hook_id=None)
        provider._delete_mirror(self.repo)
        assert path.is_dir(), "another organization still uses the mirror"
        self.repo.delete()
        provider.on_delete_repository(shared)
        assert not path.exists()

    @responses.activate
    @blame_settings
    def test_deselecting_a_repository_deletes_its_mirror(self):
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        path = mirror_path(self.mirrors, self.integration.id, 41)
        assert path.is_dir()
        api = self.server.url + "/api/v1"
        responses.get(
            api + "/repos/search", json={"data": [WEBHOOK_REPO]}, headers={"X-Total-Count": "1"}
        )
        responses.delete(api + "/repos/team/repo/hooks/4", status=204)
        with self.tasks():
            self.installation.update_organization_config({"repository_ids": []})
        with (
            self.tasks(),
            patch("sentry.deletions.defaults.repository.notify_seer_repository_deleted"),
        ):
            run_scheduled_deletions()
        assert not path.exists()

    @responses.activate
    @blame_settings
    def test_adding_a_repository_clones_its_mirror(self):
        self.mock_lookup()
        provider = GiteaRepositoryProvider("integrations:gitea")
        with self.tasks(), self.capture_on_commit_callbacks(execute=True):
            provider.on_create_repository(self.repo, self.organization)
        assert mirror_path(self.mirrors, self.integration.id, 41).is_dir()

    @responses.activate
    @blame_settings
    def test_commit_context_for_another_providers_mapping_leaves_gitea_alone_with_blame_off(self):
        # Sentry reaches the Gitea mapping whenever another provider of the project qualifies.
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        stamp = mirror_path(self.mirrors, self.integration.id, 41) / FETCHED_STAMP
        os.utime(stamp, (0, 0))
        requests = len(self.server.authorizations), len(responses.calls)
        frames = [{"filename": "app.py", "abs_path": "app.py", "lineno": 2, "in_app": True}]
        with (
            override_settings(GITEA_BLAME_ENABLED=False),
            patch("sentry_gitea.integration.tasks.mirror_repository.delay") as queued,
            patch("sentry_gitea.mirror._spawn") as git,
        ):
            blame, install = find_commit_context_for_event_all_frames(
                code_mappings=[self.code_mapping],
                frames=frames,
                organization_id=self.organization.id,
                project_id=self.project.id,
                platform="python",
                sdk_name=None,
                commit_date_max=timezone.now(),
                extra={"event": "a" * 32, "group": 1},
            )
        assert (blame, install) == (None, None)
        assert not queued.called and not git.called
        assert (len(self.server.authorizations), len(responses.calls)) == requests

    @responses.activate
    @blame_settings
    def test_a_mirror_task_queued_before_blame_was_turned_off_does_nothing(self):
        self.mock_lookup()
        with override_settings(GITEA_BLAME_ENABLED=False):
            mirror_repository(repository_id=self.repo.id)
        assert not self.mirrors.exists()
        assert self.server.authorizations == []
