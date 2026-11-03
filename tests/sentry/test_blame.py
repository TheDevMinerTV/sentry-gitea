"""Blame-based suspect commits from local mirrors, against a real repository served over HTTP."""

import fcntl
import functools
import json
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
import responses
from django.test import override_settings
from django.utils import timezone
from git_fixtures import ALICE, BOB, GIT_AVAILABLE, git, serve, source_repo
from sentry.deletions.tasks.scheduled import run_scheduled_deletions
from sentry.integrations.source_code_management.commit_context import (
    CommitContextIntegration,
    SourceLineInfo,
)
from sentry.integrations.utils.commit_context import find_commit_context_for_event_all_frames
from sentry.locks import locks
from sentry.models.commit import Commit
from sentry.models.groupowner import GroupOwner, GroupOwnerType
from sentry.tasks import post_process
from sentry.tasks.commit_context import TASK_DURATION_S, process_commit_context
from sentry.testutils.silo import no_silo_test
from sync_fixtures import WEBHOOK_REPO, SyncFixture

from sentry_gitea import mirror as mirror_module
from sentry_gitea.api import client as api_client_module
from sentry_gitea.api.client import TIMEOUT
from sentry_gitea.integration import client as integration_client_module
from sentry_gitea.integration.blame import BLAME_BUDGET
from sentry_gitea.integration.provider import GiteaIntegrationProvider
from sentry_gitea.integration.repository import GiteaRepositoryProvider
from sentry_gitea.integration.tasks import mirror_repository
from sentry_gitea.mirror import FETCHED_STAMP, LOCK_TIMEOUT, SYNCED_STAMP, mirror_path

TOKEN = "synthetic-integration-access"


class SlowClock:
    """A monotonic clock that the slow fakes below move forward instead of really waiting."""

    def __init__(self):
        self.now = 1000.0
        self.slept = 0.0
        self.on_sleep = lambda: None

    def monotonic(self):
        return self.now

    def time(self):
        return time.time()

    def sleep(self, seconds):
        self.now += seconds
        self.slept += seconds
        self.on_sleep()


class SlowGit:
    """Runs git for real, then takes every second its timeout allows, as a hanging git would."""

    def __init__(self, process, clock):
        self.process = process
        self.clock = clock

    def communicate(self, input=None, timeout=None):
        output = self.process.communicate(input)
        self.clock.now += timeout
        return output

    def __getattr__(self, name):
        return getattr(self.process, name)


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

    @responses.activate
    @blame_settings
    def test_deleting_a_repository_with_blame_off_creates_no_mirror_directory(self):
        with override_settings(GITEA_BLAME_ENABLED=False):
            GiteaRepositoryProvider("integrations:gitea")._delete_mirror(self.repo)
        assert not self.mirrors.exists()

    @responses.activate
    @blame_settings
    def test_a_mirror_from_before_blame_was_off_is_deleted_and_recloned_fresh(self):
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        path = mirror_path(self.mirrors, self.integration.id, 41)
        assert path.is_dir()
        with override_settings(GITEA_BLAME_ENABLED=False):
            GiteaRepositoryProvider("integrations:gitea")._delete_mirror(self.repo)
        assert not path.exists()
        pushed = self.source.commit(
            "Rework b", {"app.py": "a\nbb\nc\n"}, author=ALICE, when=timezone.now()
        )
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        blamed = self.installation.get_blame_for_files([self.line(2)], extra={})
        assert [b.commit.commitId for b in blamed] == [pushed]

    @responses.activate
    @blame_settings
    def test_a_push_while_blame_is_off_still_makes_the_next_blame_fetch(self):
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        pushed = self.source.commit(
            "Rework b", {"app.py": "a\nbb\nc\n"}, author=ALICE, when=timezone.now()
        )
        with override_settings(GITEA_BLAME_ENABLED=False):
            assert self.signed("push", self.push_payload()).status_code == 204
        blamed = self.installation.get_blame_for_files([self.line(2)], extra={})
        assert [b.commit.commitId for b in blamed] == [pushed]

    @responses.activate
    @blame_settings
    def test_the_mirror_task_keeps_only_the_default_branch_and_mapped_refs(self):
        self.mock_lookup()
        first = self.source.commits["Add app"]
        self.source.branch("feature", first)
        self.source.tag("v1", first, annotated=True)
        self.source.tag("v2", first)
        other = self.create_project(organization=self.organization)
        self.create_code_mapping(
            project=other,
            repo=self.repo,
            organization_integration=self.org_integration,
            default_branch="v1",
            stack_root="other/",
        )
        mirror_repository(repository_id=self.repo.id)
        path = mirror_path(self.mirrors, self.integration.id, 41)
        listed = git(path, "for-each-ref", "--format=%(refname)").splitlines()
        assert sorted(listed) == ["refs/heads/main", "refs/tags/v1"]
        blamed = self.installation.get_blame_for_files([self.line(1, ref="v1")], extra={})
        assert [b.commit.commitId for b in blamed] == [first]

    def worst_case_blame_seconds(self, refs):
        """Blame `refs` while every step takes all the time it is allowed: each git command its
        full timeout, the lock its full wait, the API lookup its connect and read timeouts."""
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        path = mirror_path(self.mirrors, self.integration.id, 41)
        os.utime(path / FETCHED_STAMP, (0, 0))
        clock = SlowClock()

        def slow_lookup(request):
            clock.now += sum(TIMEOUT)
            return 200, {}, json.dumps(WEBHOOK_REPO)

        lookup = self.server.url + "/api/v1/repositories/41"
        responses.remove(responses.GET, lookup)
        responses.add_callback(responses.GET, lookup, callback=slow_lookup)
        holder = open(path.with_suffix(".lock"), "a")
        self.addCleanup(holder.close)
        fcntl.flock(holder, fcntl.LOCK_EX)

        def release_at_the_last_moment():
            if clock.slept >= LOCK_TIMEOUT - 0.5:
                fcntl.flock(holder, fcntl.LOCK_UN)

        clock.on_sleep = release_at_the_last_moment
        spawn = mirror_module._spawn
        files = [self.line(1, ref=ref) for ref in refs]
        files += [self.line(line, path=name) for name in ("app.py", "b.py") for line in (1, 3)]
        with (
            patch.object(mirror_module, "time", clock),
            patch.object(api_client_module, "time", clock),
            patch.object(integration_client_module, "time", clock),
            patch.object(mirror_module, "_spawn", lambda *a: SlowGit(spawn(*a), clock)),
            patch("sentry_gitea.integration.tasks.mirror_repository.delay"),
        ):
            started = clock.now
            self.installation.get_blame_for_files(files, extra={})
            return clock.now - started

    @responses.activate
    @blame_settings
    def test_a_slow_lock_lookup_and_fetch_end_within_the_blame_budget(self):
        elapsed = self.worst_case_blame_seconds(["main"])
        assert BLAME_BUDGET - 1 <= elapsed <= BLAME_BUDGET < TASK_DURATION_S

    @responses.activate
    @blame_settings
    def test_a_lookup_that_starts_late_cannot_outlast_the_blame_budget(self):
        # Two slow local reads and the lock wait leave too little time for the lookup.
        elapsed = self.worst_case_blame_seconds(["1" * 40, "2" * 40, "main"])
        assert 20 < elapsed <= BLAME_BUDGET

    @responses.activate
    @blame_settings
    def test_local_reads_stop_at_the_blame_budget(self):
        elapsed = self.worst_case_blame_seconds([str(digit) * 40 for digit in range(1, 6)])
        assert BLAME_BUDGET - 1 <= elapsed <= BLAME_BUDGET

    @responses.activate
    @blame_settings
    def test_the_lock_wait_stops_at_the_blame_budget(self):
        elapsed = self.worst_case_blame_seconds([str(digit) * 40 for digit in range(1, 5)])
        assert BLAME_BUDGET - 1 <= elapsed <= BLAME_BUDGET

    def expire_token(self):
        """The stored access token has expired; Gitea accepts only the one a refresh returns."""
        self.identity.update(
            external_id=f"{self.server.url}|2",
            data={
                **self.identity.data,
                "access_token": "expired",
                "expires": int(time.time()) - 60,
            },
        )

        def lookup(request):
            if request.headers.get("Authorization") != f"Bearer {TOKEN}":
                return 401, {}, json.dumps({"message": "token expired"})
            return 200, {}, json.dumps(WEBHOOK_REPO)

        url = self.server.url + "/api/v1/repositories/41"
        if any(r.url == url for r in responses.registered()):
            responses.remove(responses.GET, url)
        responses.add_callback(responses.GET, url, callback=lookup)
        responses.post(
            self.server.url + "/login/oauth/access_token",
            json={"access_token": TOKEN, "refresh_token": "rotated", "expires_in": 3600},
        )

    @responses.activate
    @blame_settings
    def test_the_mirror_task_refreshes_an_expired_token(self):
        self.expire_token()
        mirror_repository(repository_id=self.repo.id)
        assert mirror_path(self.mirrors, self.integration.id, 41).is_dir()
        assert set(self.server.authorizations) == {f"Bearer {TOKEN}"}

    @responses.activate
    @blame_settings
    def test_blame_refreshes_an_expired_token_and_fetches(self):
        # Gitea access tokens last an hour: an idle installation must not lose every first blame.
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        pushed = self.source.commit(
            "Rework b", {"app.py": "a\nbb\nc\n"}, author=ALICE, when=timezone.now()
        )
        os.utime(mirror_path(self.mirrors, self.integration.id, 41) / FETCHED_STAMP, (0, 0))
        self.expire_token()
        with patch("sentry_gitea.integration.tasks.mirror_repository.delay") as queued:
            blamed = self.installation.get_blame_for_files([self.line(2)], extra={})
        assert [b.commit.commitId for b in blamed] == [pushed]
        assert not queued.called

    @responses.activate
    @blame_settings
    def test_a_held_refresh_lock_cannot_hold_blame_past_its_budget(self):
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        os.utime(mirror_path(self.mirrors, self.integration.id, 41) / FETCHED_STAMP, (0, 0))
        self.expire_token()
        held = locks.get(
            f"sentry-gitea:identity:{self.identity.id}", duration=90, name="gitea_token_refresh"
        )
        held.acquire()
        self.addCleanup(held.release)
        budget = 15 + sum(TIMEOUT) + 3  # read slack and one request, leaving 3 s to wait
        started = time.monotonic()
        with (
            patch("sentry_gitea.integration.blame.BLAME_BUDGET", budget),
            patch("sentry_gitea.integration.tasks.mirror_repository.delay") as queued,
        ):
            assert self.installation.get_blame_for_files([self.line(2)], extra={}) == []
        assert time.monotonic() - started < budget
        assert queued.called, "the task, with time to wait, refreshes instead"

    @responses.activate
    @blame_settings
    def test_blame_on_a_mirror_from_an_earlier_version_queues_its_sync(self):
        self.mock_lookup()
        with self.tasks():
            self.installation.get_blame_for_files([self.line(2)], extra={})
        path = mirror_path(self.mirrors, self.integration.id, 41)
        (path / SYNCED_STAMP).unlink()
        with self.tasks():
            blamed = self.installation.get_blame_for_files([self.line(2)], extra={})
        assert [b.commit.commitId for b in blamed] == [self.source.commits["Fix b"]]
        assert (path / SYNCED_STAMP).exists()
