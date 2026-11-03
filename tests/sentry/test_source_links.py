"""RepositoryIntegration: stack-trace links, source URL parsing and repository metadata."""

from unittest.mock import patch

import responses
from rest_framework.exceptions import ValidationError
from sentry.integrations.source_code_management.repository import (
    RepositoryClient,
    RepositoryIntegration,
)
from sentry.models.group import Group
from sentry.models.repository import Repository
from sentry.shared_integrations.exceptions import ApiForbiddenError
from sentry.testutils.silo import no_silo_test
from sync_fixtures import (
    CONTENTS_EXT,
    SHA_A,
    WEBHOOK_REPO,
    SyncFixture,
    contents_ext_dir,
    contents_ext_file,
)
from test_integration_provider import API, PUBLIC, REPO

# Metadata only (no file content): Gitea 28 `contents-ext` without `includes`.
CONTENTS = CONTENTS_EXT
SOURCE = PUBLIC + "/team/repo/src/"


@no_silo_test
class TestSourceLinks(SyncFixture):
    def setUp(self):
        super().setUp()
        self.repo = self.add_repo()

    def mock_lookup(self, **changes):
        responses.get(API + "/repositories/41", json={**WEBHOOK_REPO, **changes})

    @responses.activate
    def test_interfaces_and_version_then_default_branch_fallback(self):
        assert isinstance(self.installation, RepositoryIntegration)
        assert isinstance(self.installation.get_client(), RepositoryClient)
        assert self.installation.get_client().base_url == API.removesuffix("/api/v1")
        self.mock_lookup(has_issues=False)
        responses.get(CONTENTS + "src/app.py", status=404)
        responses.get(CONTENTS + "src/app.py", json=contents_ext_file("src/app.py"))
        link = self.installation.get_stacktrace_link(self.repo, "src/app.py", "main", SHA_A)
        assert link == SOURCE + "branch/main/src/app.py"
        refs = [
            c.request.url.split("?ref=")[1] for c in responses.calls if "contents" in c.request.url
        ]
        assert refs == [SHA_A, "main"]
        # Repository lookups are cached for the installation instance.
        assert len([c for c in responses.calls if "/repositories/" in c.request.url]) == 1
        responses.replace(
            responses.GET, CONTENTS + "src/app.py", json=contents_ext_file("src/app.py")
        )
        assert (
            self.installation.get_stacktrace_link(self.repo, "src/app.py", "main", SHA_A)
            == SOURCE + f"commit/{SHA_A}/src/app.py"
        )

    @responses.activate
    def test_missing_directory_and_forbidden_files(self):
        self.mock_lookup()
        responses.get(CONTENTS + "missing.py", status=404)
        responses.get(CONTENTS + "folder", json=contents_ext_dir("folder"))
        responses.get(CONTENTS + "link.py", json=contents_ext_file("link.py", "symlink"))
        responses.get(CONTENTS + "secret.py", status=403)
        assert self.installation.get_stacktrace_link(self.repo, "missing.py", "main", None) is None
        assert self.installation.get_stacktrace_link(self.repo, "folder", "main", None) is None
        assert self.installation.get_stacktrace_link(self.repo, "link.py", "main", None) is None
        with self.assertRaises(ApiForbiddenError):
            self.installation.get_stacktrace_link(self.repo, "secret.py", "main", None)
        # Traversal never reaches Gitea and never links.
        calls = len(responses.calls)
        assert (
            self.installation.get_stacktrace_link(self.repo, "../etc/passwd", "main", None) is None
        )
        assert len(responses.calls) == calls

    def test_format_source_url_encodes_and_stays_inside_the_repository(self):
        fmt = self.installation.format_source_url
        assert fmt(self.repo, "src/a b#c.py", SHA_A) == SOURCE + f"commit/{SHA_A}/src/a%20b%23c.py"
        assert (
            fmt(self.repo, "src/app.py", "release/1.0") == SOURCE + "branch/release/1.0/src/app.py"
        )
        assert fmt(self.repo, "../../../../x.py", "main") == SOURCE + "branch/main/x.py"
        assert fmt(self.repo, "a/./b/../c.py", "../main") == SOURCE + "branch/main/a/c.py"
        assert fmt(self.repo, "src/app.py", None) == SOURCE + "branch/main/src/app.py"
        self.repo.update(config={**self.repo.config, "default_branch": "trunk"})
        assert fmt(self.repo, "app.py", "") == SOURCE + "branch/trunk/app.py"

    @responses.activate
    def test_stacktrace_link_endpoint_uses_code_mappings(self):
        self.mock_lookup()
        self.create_code_mapping(
            self.project,
            repo=self.repo,
            organization_integration=self.org_integration,
            stack_root="/app/",
            source_root="src/",
            default_branch="main",
        )
        responses.get(CONTENTS + "src/app.py", json=contents_ext_file("src/app.py"))
        responses.get(CONTENTS + "src/gone.py", status=404)
        url = f"/api/0/projects/{self.organization.slug}/{self.project.slug}/stacktrace-link/"
        with patch.object(Group, "get_latest_event", return_value=None):
            found = self.client.get(url, {"file": "/app/app.py"})
            missing = self.client.get(url, {"file": "/app/gone.py"})
        assert found.status_code == 200, found.data
        assert found.data["sourceUrl"] == SOURCE + "branch/main/src/app.py"
        assert missing.status_code == 200 and missing.data["sourceUrl"] is None
        assert missing.data["error"] == "file_not_found"
        assert missing.data["attemptedUrl"] == SOURCE + "branch/main/src/gone.py"

    @responses.activate
    def test_renamed_and_bulk_mapping_repositories_resolve_current_paths(self):
        self.mock_lookup(full_name="team/renamed")
        responses.get(
            API + "/repos/team/renamed/contents-ext/app.py", json=contents_ext_file("app.py")
        )
        assert self.installation.get_client().check_file(self.repo, "app.py", "main")
        # Bulk code mappings create rows like this one: path-valued external_id, no config.
        bulk = Repository.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            provider="integrations:gitea",
            name="team/other",
            external_id="team/other",
        )
        responses.get(
            API + "/repos/team/other", json={**WEBHOOK_REPO, "id": 42, "full_name": "team/other"}
        )
        responses.get(
            API + "/repos/team/other/contents-ext/app.py", json=contents_ext_file("app.py")
        )
        assert self.installation.get_client().check_file(bulk, "app.py", None)
        # Read paths do not rewrite Repository rows.
        bulk.refresh_from_db()
        assert bulk.external_id == "team/other" and bulk.config == {}

    @responses.activate
    def test_repositories_outside_the_owner_are_never_read(self):
        self.mock_lookup(owner={"id": 99, "login": "elsewhere"})
        assert self.installation.get_client().check_file(self.repo, "app.py", "main") is None
        assert not self.installation.has_repo_access(self.repo)
        assert all("/contents" not in c.request.url for c in responses.calls)

    def test_source_url_matching_is_scoped_to_public_url_and_owner(self):
        matches = self.installation.source_url_matches
        url = SOURCE + "branch/main/src/app.py#L10"
        assert matches(url) and matches(SOURCE + f"commit/{SHA_A}/a.py")
        assert matches(SOURCE + "tag/v1.0/a.py")
        for bad in [
            url.replace("https:", "http:"),
            url.replace("/git/", "/git-other/"),
            url.replace("public-forge.example", "evil.example"),
            url.replace("public-forge.example", "public-forge.example.evil.example"),
            PUBLIC + "/elsewhere/repo/src/branch/main/a.py",
            PUBLIC + "/team/repo/raw/branch/main/a.py",
            PUBLIC + "/team/repo/src/branch/main",
            PUBLIC + "/team/repo/src/branch/%2e%2e/x/y.py",
            "https://forge.example/git/team/repo/src/branch/main/a.py",  # internal base URL
            "not a url",
        ]:
            assert not matches(bad), bad
        Repository.objects.create(
            organization_id=self.organization.id,
            integration_id=self.integration.id,
            provider="integrations:gitea",
            name="former/repo",
            external_id="77",
        )
        assert matches(PUBLIC + "/former/repo/src/branch/main/a.py")

    @responses.activate
    def test_branch_names_with_slashes_tags_and_commits_are_parsed(self):
        self.mock_lookup()
        branches = API + "/repos/team/repo/branches/"
        responses.get(branches + "release", status=404)
        responses.get(branches + "release/one", json={"name": "release/one"})
        url = SOURCE + "branch/release/one/src/app.py"
        assert self.installation.extract_branch_from_source_url(self.repo, url) == "release/one"
        assert self.installation.extract_source_path_from_source_url(self.repo, url) == "src/app.py"
        responses.get(API + "/repos/team/repo/tags/v1.0", json={"name": "v1.0"})
        tag = SOURCE + "tag/v1.0/lib/x.py"
        assert self.installation.extract_branch_from_source_url(self.repo, tag) == "v1.0"
        commit = SOURCE + f"commit/{SHA_A}/lib/x%20y.py"
        assert self.installation.extract_branch_from_source_url(self.repo, commit) == SHA_A
        assert (
            self.installation.extract_source_path_from_source_url(self.repo, commit) == "lib/x y.py"
        )
        with self.assertRaises(ValidationError):
            self.installation.extract_branch_from_source_url(
                self.repo, PUBLIC + "/team/other/src/branch/main/a.py"
            )

    @responses.activate
    def test_repo_path_parsing_endpoint_derives_a_code_mapping(self):
        self.mock_lookup()
        responses.get(API + "/repos/team/repo/branches/main", json={"name": "main"})
        result = self.client.post(
            f"/api/0/projects/{self.organization.slug}/{self.project.slug}/repo-path-parsing/",
            {"sourceUrl": SOURCE + "branch/main/src/pkg/app.py", "stackPath": "pkg/app.py"},
            format="json",
        )
        assert result.status_code == 200, result.data
        assert result.data["integrationId"] == self.integration.id
        assert result.data["repositoryId"] == self.repo.id
        assert result.data["provider"] == "gitea" and result.data["defaultBranch"] == "main"
        assert result.data["sourceRoot"].startswith("src/")

    @responses.activate
    def test_repositories_include_code_only_but_issue_forms_do_not(self):
        responses.get(
            API + "/repos/search",
            json={
                "data": [{**REPO, "has_issues": False}, {**REPO, "id": 42, "full_name": "team/b"}]
            },
            headers={"X-Total-Count": "2"},
        )
        rows = self.installation.get_repositories("team")
        assert [r["identifier"] for r in rows] == ["team/repo", "team/b"]
        assert rows[0] == {
            "identifier": "team/repo",
            "name": "team/repo",
            "external_id": "41",
            "default_branch": "main",
        }
        assert self.installation.get_repository_choices(None, {}) == (
            "team/b",
            [("team/b", "team/b")],
        )
        result = self.client.get(
            f"/api/0/organizations/{self.organization.slug}/integrations/"
            f"{self.integration.id}/repos/",
            {"search": "team"},
        )
        assert result.status_code == 200, result.data
        assert {r["identifier"]: r["isInstalled"] for r in result.data["repos"]} == {
            "team/repo": True,
            "team/b": False,
        }

    @responses.activate
    def test_default_branch_and_access_lookups(self):
        self.mock_lookup(default_branch="trunk")
        assert self.installation.get_repository_default_branch(self.repo) == "trunk"
        assert self.installation.has_repo_access(self.repo)
